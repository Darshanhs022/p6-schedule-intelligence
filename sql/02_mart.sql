-- =====================================================================
-- Analytical layer. Every metric is defined ONCE here, so a number on a
-- dashboard can always be traced to a definition rather than to whatever
-- a report author typed. Views, not tables: the facts are the truth.
-- =====================================================================
 
-- ---------------------------------------------------------------------
-- WORKING-DAY ARITHMETIC
-- Every date difference below is expressed BOTH ways. Calendar days are what
-- a client sees on a letter; working days are what the site actually lost.
-- On a 6-day week with 20 site holidays the two differ by roughly 15%, so
-- reporting only calendar days overstates every slip on the chart.
--
-- workday_index is a running count of working days, so the difference between
-- two dates is a subtraction rather than a date walk.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_activity_calendar AS
SELECT a.activity_key, a.activity_id, a.project_key, a.calendar_key
FROM core.dim_activity a
WHERE a.is_current;

-- Working days between two dates on a given calendar. NULL if either date
-- falls outside the built calendar range, which is a signal, not a silent zero.

CREATE OR REPLACE FUNCTION mart.workdays_between(
    p_calendar_key INT, p_from DATE, p_to DATE) RETURNS INT AS $$
    SELECT (SELECT workday_index FROM core.dim_calendar_day
             WHERE calendar_key = p_calendar_key AND cal_date = p_to)
         - (SELECT workday_index FROM core.dim_calendar_day
             WHERE calendar_key = p_calendar_key AND cal_date = p_from);
$$ LANGUAGE sql STABLE;


-- Latest snapshot per project ------------------------------------------
CREATE OR REPLACE VIEW mart.v_current_snapshot AS
SELECT DISTINCT ON (project_key) *
FROM core.dim_snapshot
WHERE snapshot_type <> 'BASELINE'
ORDER BY project_key, data_date DESC;

-- The baseline each snapshot is measured against ------------------------
CREATE OR REPLACE VIEW mart.v_baseline_snapshot AS
SELECT DISTINCT ON (project_key) *
FROM core.dim_snapshot
WHERE snapshot_type = 'BASELINE'
ORDER BY project_key, data_date ASC;

select * from core.dim_snapshot;

-- Activity fact enriched with dimensions --------------------------------
CREATE OR REPLACE VIEW mart.v_activity AS
SELECT f.*,
       s.data_date          AS snap_date,
       s.snapshot_type,
       s.snapshot_seq,
       a.activity_id, a.activity_name, a.activity_type, a.is_milestone,
       w.wbs_code, w.wbs_name, w.wbs_path, w.area, w.phase, w.wbs_level,
       p.project_id, p.currency_code, p.value_basis
FROM core.fact_activity_snapshot f
JOIN core.dim_snapshot  s ON s.snapshot_key = f.snapshot_key
JOIN core.dim_activity  a ON a.activity_key = f.activity_key
JOIN core.dim_wbs       w ON w.wbs_key      = f.wbs_key
JOIN core.dim_project   p ON p.project_key  = f.project_key;

-- ---------------------------------------------------------------------
-- BASELINE VARIANCE
-- Working-day variance via dim_calendar_day. Calendar-day arithmetic is
-- wrong on a 6-day week with site holidays.
-- ---------------------------------------------------------------------
-- NOTE ON KEYS: dim_activity is SCD2, so renaming an activity or moving it
-- between WBS nodes mints a NEW activity_key. Joining or partitioning on that
-- surrogate would silently break the series at the point of change and drop
-- the activity's budget from earned value. Every trend and baseline join below
-- therefore uses the NATURAL key, activity_id.

CREATE OR REPLACE VIEW mart.v_activity_variance AS
WITH bl AS (
    SELECT a.activity_id, f.early_start AS bl_start, f.early_finish AS bl_finish,
           f.original_duration_d AS bl_duration, f.budget_value AS bac,
           f.total_float_d AS bl_float
    FROM core.fact_activity_snapshot f
    JOIN mart.v_baseline_snapshot b ON b.snapshot_key = f.snapshot_key
    JOIN core.dim_activity a        ON a.activity_key = f.activity_key
)
SELECT c.*,
       bl.bl_start, bl.bl_finish, bl.bl_duration, bl.bac,
       (COALESCE(c.actual_finish, c.early_finish)::date - bl.bl_finish::date) AS finish_var_cal_d,
       mart.workdays_between(da.calendar_key, bl.bl_finish::date,
                             COALESCE(c.actual_finish, c.early_finish)::date) AS finish_var_work_d,
       (COALESCE(c.actual_start, c.early_start)::date - bl.bl_start::date)    AS start_var_cal_d,
       mart.workdays_between(da.calendar_key, bl.bl_start::date,
                             COALESCE(c.actual_start, c.early_start)::date)   AS start_var_work_d,
       (c.at_completion_dur_d - bl.bl_duration)                               AS duration_var_d
FROM mart.v_activity c
JOIN bl ON bl.activity_id = c.activity_id
LEFT JOIN core.dim_activity da ON da.activity_key = c.activity_key
WHERE c.snapshot_type <> 'BASELINE';

-- ---------------------------------------------------------------------
-- EARNED VALUE
-- PV is baseline value that SHOULD be earned by the data date, prorated
-- across each baseline activity's span. EV is actual progress x budget.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_evm_activity AS
WITH bl AS (
    SELECT a.activity_id, f.early_start AS bl_start, f.early_finish AS bl_finish,
           f.budget_value AS bac
    FROM core.fact_activity_snapshot f
    JOIN mart.v_baseline_snapshot b ON b.snapshot_key = f.snapshot_key
    JOIN core.dim_activity a        ON a.activity_key = f.activity_key
)
SELECT c.snapshot_key, c.snap_date, c.project_key, c.activity_key, c.activity_id,
       c.area, c.phase, c.wbs_path, c.status,
       bl.bac,
       CASE
         WHEN bl.bl_finish::date <= c.snap_date THEN bl.bac
         WHEN bl.bl_start::date  >  c.snap_date THEN 0
         ELSE bl.bac * ( GREATEST(mart.workdays_between(da.calendar_key,
                                    bl.bl_start::date, c.snap_date), 0)::numeric
                       / NULLIF(mart.workdays_between(da.calendar_key,
                                    bl.bl_start::date, bl.bl_finish::date), 0) )
       END                                    AS planned_value,
       c.earned_value,
       c.pct_complete
FROM mart.v_activity c
JOIN bl ON bl.activity_id = c.activity_id
LEFT JOIN core.dim_activity da ON da.activity_key = c.activity_key
where snapshot_key <> 1;

CREATE OR REPLACE VIEW mart.v_evm_project AS
SELECT snap_date, project_key,
       SUM(bac)                                             AS bac,
       SUM(planned_value)                                   AS pv,
       SUM(earned_value)                                    AS ev,
       ROUND(SUM(earned_value) / NULLIF(SUM(planned_value),0), 4) AS spi,
       ROUND(SUM(earned_value) / NULLIF(SUM(bac),0), 4)     AS pct_complete,
       SUM(earned_value) - SUM(planned_value)               AS schedule_variance
FROM mart.v_evm_activity
GROUP BY snap_date, project_key;

select * from mart.v_evm_by_area;

CREATE OR REPLACE VIEW mart.v_evm_by_area AS
SELECT snap_date, project_key, area,
       SUM(bac) AS bac,
	   SUM(planned_value) AS pv,
	   SUM(earned_value) AS ev,
       ROUND(SUM(earned_value)/NULLIF(SUM(planned_value),0), 4) AS spi
FROM mart.v_evm_activity
WHERE area IS NOT NULL
GROUP BY snap_date, project_key, area
order by snap_date;

-- ---------------------------------------------------------------------
-- COST  (ERP feed, not the scheduler)
-- ---------------------------------------------------------------------
select * from mart.v_cost_performance;
CREATE OR REPLACE VIEW mart.v_cost_performance AS
WITH ac AS (
    SELECT project_key, activity_key, period_end, SUM(actual_cost) AS actual_cost
    FROM core.fact_actual_cost
    GROUP BY project_key, activity_key, period_end
)
SELECT e.snap_date, e.project_key,
       SUM(e.bac) AS bac, SUM(e.earned_value) AS ev,
       SUM(COALESCE(a.actual_cost,0)) AS ac,
       ROUND(SUM(e.earned_value)/NULLIF(SUM(a.actual_cost),0), 4) AS cpi,
       SUM(e.earned_value) - SUM(COALESCE(a.actual_cost,0))       AS cost_variance,
       ROUND(SUM(e.bac) / NULLIF(SUM(e.earned_value)/NULLIF(SUM(a.actual_cost),0),0), 2) AS eac
FROM mart.v_evm_activity e
LEFT JOIN ac a ON a.activity_key = e.activity_key AND a.period_end <= e.snap_date
GROUP BY e.snap_date, e.project_key;

-- ---------------------------------------------------------------------
-- MILESTONE SLIP CHART
-- Forecast finish of each milestone by data date. The single most
-- recognised chart in project controls, and it needs only snapshots.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_milestone_trend AS
WITH bl AS (
    SELECT a.activity_id, f.early_finish AS bl_finish
    FROM core.fact_activity_snapshot f
    JOIN mart.v_baseline_snapshot b ON b.snapshot_key = f.snapshot_key
    JOIN core.dim_activity a        ON a.activity_key = f.activity_key
)
SELECT c.project_key, c.snap_date, c.activity_id, c.activity_name, c.area,
       COALESCE(c.actual_finish, c.early_finish)::date AS forecast_finish,
       bl.bl_finish::date                              AS baseline_finish,
       (COALESCE(c.actual_finish, c.early_finish)::date - bl.bl_finish::date) AS slip_cal_days,
       mart.workdays_between(da.calendar_key, bl.bl_finish::date,
                COALESCE(c.actual_finish, c.early_finish)::date)              AS slip_work_days,
       c.status
FROM mart.v_activity c
JOIN bl ON bl.activity_id = c.activity_id
LEFT JOIN core.dim_activity da ON da.activity_key = c.activity_key
WHERE c.is_milestone AND c.snapshot_type <> 'BASELINE';

-- ---------------------------------------------------------------------
-- FLOAT EROSION
-- Float lost week on week, BEFORE anything is actually late. The one
-- signal a conventional status report cannot produce.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_float_erosion AS
SELECT c.project_key, c.snap_date, c.activity_key, c.activity_id, c.area, c.phase,
       c.total_float_d,
       LAG(c.total_float_d) OVER (PARTITION BY c.activity_id ORDER BY c.snap_date) AS prev_float_d,
       c.total_float_d
         - LAG(c.total_float_d) OVER (PARTITION BY c.activity_id ORDER BY c.snap_date) AS float_delta_d,
       c.status
FROM mart.v_activity c
WHERE c.status <> 'COMPLETE';

select * from mart.v_float_erosion_by_area ;

CREATE OR REPLACE VIEW mart.v_float_erosion_by_area AS
SELECT project_key, snap_date, area,
       ROUND(AVG(total_float_d), 1)                    AS avg_float_d,
       ROUND(AVG(float_delta_d), 2)                    AS avg_float_change_d,
       COUNT(*) FILTER (WHERE float_delta_d < -5)      AS activities_losing_float,
       COUNT(*) FILTER (WHERE total_float_d < 0)       AS activities_negative_float
FROM mart.v_float_erosion
WHERE area IS NOT NULL AND prev_float_d IS NOT NULL
GROUP BY project_key, snap_date, area;

-- ---------------------------------------------------------------------
-- BEI and CPLI  (DCMA)
-- BEI  = activities actually completed / baseline said should be complete
-- CPLI = (remaining duration to milestone + total float) / remaining duration
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_bei AS
WITH bl AS (
    SELECT a.activity_id, f.early_finish::date AS bl_finish
    FROM core.fact_activity_snapshot f
    JOIN mart.v_baseline_snapshot b ON b.snapshot_key = f.snapshot_key
    JOIN core.dim_activity a        ON a.activity_key = f.activity_key
)
SELECT c.project_key, c.snap_date,
       COUNT(*) FILTER (WHERE c.status = 'COMPLETE')                  AS completed,
       COUNT(*) FILTER (WHERE bl.bl_finish <= c.snap_date)            AS should_be_complete,
       ROUND(COUNT(*) FILTER (WHERE c.status = 'COMPLETE')::numeric
             / NULLIF(COUNT(*) FILTER (WHERE bl.bl_finish <= c.snap_date), 0), 4) AS bei
FROM mart.v_activity c
JOIN bl ON bl.activity_id = c.activity_id
WHERE c.snapshot_type <> 'BASELINE'
GROUP BY c.project_key, c.snap_date;

-- ---------------------------------------------------------------------
-- SCHEDULE QUALITY (DCMA 14, the checks this data supports)
-- ---------------------------------------------------------------------

CREATE OR REPLACE VIEW mart.v_schedule_quality AS
WITH base AS (
    SELECT f.snapshot_key, f.project_key, s.data_date, f.activity_key,
           f.total_float_d, f.original_duration_d, f.status,
           f.constraint_type, a.is_milestone
    FROM core.fact_activity_snapshot f
    JOIN core.dim_snapshot s ON s.snapshot_key = f.snapshot_key
    JOIN core.dim_activity a ON a.activity_key = f.activity_key
), links AS (
    SELECT snapshot_key,
           COUNT(*)                                        AS n_links,
           COUNT(*) FILTER (WHERE lag_d < 0)               AS n_negative_lag,
           COUNT(*) FILTER (WHERE relationship_type = 'FS') AS n_fs,
           COUNT(*) FILTER (WHERE relationship_type = 'SF') AS n_sf
    FROM core.fact_relationship_snapshot GROUP BY snapshot_key
), has_pred AS (
    SELECT DISTINCT snapshot_key, succ_activity_key AS activity_key
    FROM core.fact_relationship_snapshot
), has_succ AS (
    SELECT DISTINCT snapshot_key, pred_activity_key AS activity_key
    FROM core.fact_relationship_snapshot
), ends AS (
    SELECT b.snapshot_key,
           COUNT(*) FILTER (WHERE NOT b.is_milestone
                              AND (p.activity_key IS NULL OR s.activity_key IS NULL)
                           ) AS n_open_ends
    FROM base b
    LEFT JOIN has_pred p ON p.snapshot_key = b.snapshot_key AND p.activity_key = b.activity_key
    LEFT JOIN has_succ s ON s.snapshot_key = b.snapshot_key AND s.activity_key = b.activity_key
    GROUP BY b.snapshot_key
)
SELECT b.project_key, b.data_date, b.snapshot_key,
       COUNT(*)                                                      AS n_activities,
       e.n_open_ends,
       ROUND(100.0*e.n_open_ends/COUNT(*), 2)                        AS pct_open_ends,
       ROUND(l.n_links::numeric/COUNT(*), 2)                         AS logic_density,
       l.n_negative_lag,
       ROUND(100.0*l.n_fs/NULLIF(l.n_links,0), 1)                    AS pct_fs_links,
       l.n_sf                                                        AS n_start_finish_links,
       COUNT(*) FILTER (WHERE b.total_float_d > 44)                  AS n_high_float,
       COUNT(*) FILTER (WHERE b.total_float_d < 0)                   AS n_negative_float,
       COUNT(*) FILTER (WHERE b.original_duration_d > 44)            AS n_long_duration,
       COUNT(*) FILTER (WHERE b.constraint_type IN ('CS_MANDSTART','CS_MANDFIN')) AS n_hard_constraints
FROM base b
JOIN links l ON l.snapshot_key = b.snapshot_key
JOIN ends  e ON e.snapshot_key = b.snapshot_key
GROUP BY b.project_key, b.data_date, b.snapshot_key, e.n_open_ends, l.n_links,
         l.n_negative_lag, l.n_fs, l.n_sf;
		

-- ---------------------------------------------------------------------
-- CHANGE REGISTERS
-- Because every snapshot is a full state, scope and logic changes are a
-- set difference. Most tools cannot answer these questions at all.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_scope_change AS
WITH pairs AS (
    SELECT s.snapshot_key, s.data_date, s.project_key,
           LAG(s.snapshot_key) OVER (PARTITION BY s.project_key ORDER BY s.snapshot_seq) AS prev_key
    FROM core.dim_snapshot s
)
SELECT p.project_key, p.data_date,
       COUNT(*) FILTER (WHERE cur.activity_key IS NULL) AS activities_removed,
       COUNT(*) FILTER (WHERE prv.activity_key IS NULL) AS activities_added
FROM pairs p
LEFT JOIN core.fact_activity_snapshot cur ON cur.snapshot_key = p.snapshot_key
FULL JOIN core.fact_activity_snapshot prv
       ON prv.snapshot_key = p.prev_key AND prv.activity_key = cur.activity_key
WHERE p.prev_key IS NOT NULL
GROUP BY p.project_key, p.data_date;


CREATE OR REPLACE VIEW mart.v_logic_change AS
WITH pairs AS (
    SELECT s.snapshot_key, s.data_date, s.project_key,
           LAG(s.snapshot_key) OVER (PARTITION BY s.project_key ORDER BY s.snapshot_seq) AS prev_key
    FROM core.dim_snapshot s
)
SELECT p.project_key, p.data_date,
       COUNT(*) FILTER (WHERE o.pred_activity_key IS NULL)                       AS links_added,
       COUNT(*) FILTER (WHERE n.pred_activity_key IS NULL)                       AS links_removed,
       COUNT(*) FILTER (WHERE n.lag_d IS DISTINCT FROM o.lag_d
                          AND n.pred_activity_key IS NOT NULL
                          AND o.pred_activity_key IS NOT NULL)                   AS lags_changed
FROM pairs p
LEFT JOIN core.fact_relationship_snapshot n ON n.snapshot_key = p.snapshot_key
FULL JOIN core.fact_relationship_snapshot o
       ON o.snapshot_key = p.prev_key
      AND o.pred_activity_key = n.pred_activity_key
      AND o.succ_activity_key = n.succ_activity_key
WHERE p.prev_key IS NOT NULL
GROUP BY p.project_key, p.data_date;

-- Executive one-liner ---------------------------------------------------
CREATE OR REPLACE VIEW mart.v_executive_summary AS
SELECT e.project_key, e.snap_date, p.project_id, p.currency_code,
       e.bac, e.pv, e.ev, e.spi, e.pct_complete, e.schedule_variance,
       b.bei,
       q.n_open_ends, q.pct_open_ends, q.logic_density,
       q.n_negative_float, q.n_high_float,
       s.project_finish
FROM mart.v_evm_project e
JOIN core.dim_project p     ON p.project_key = e.project_key
LEFT JOIN mart.v_bei b      ON b.project_key = e.project_key AND b.snap_date = e.snap_date
LEFT JOIN core.dim_snapshot s ON s.project_key = e.project_key AND s.data_date = e.snap_date
                             AND s.snapshot_type <> 'BASELINE'
LEFT JOIN mart.v_schedule_quality q ON q.snapshot_key = s.snapshot_key;

-- =====================================================================
-- PROCUREMENT BUFFER ANALYSIS
--
-- The metric that matters is BUFFER: how many days sit between a material
-- arriving and the first activity that needs it. A shrinking buffer is the
-- earliest actionable warning in procurement — it moves weeks before any
-- activity is late, which is exactly why a conventional status report
-- never catches it. 
-- =====================================================================

-- Procurement activities and the work they feed --------------------------
CREATE OR REPLACE VIEW mart.v_procurement_link AS
SELECT
    v.snapshot_key,
    v.snap_date,
    v.project_key,
    v.activity_key                                  AS supply_key,
    v.activity_id                                   AS supply_id,
    v.wbs_name || ' - ' ||
      TRIM(REGEXP_REPLACE(v.activity_name, '\s*\([^)]*\)\s*', ' ', 'g'))
                                                    AS supply_name,
    v.activity_name                                 AS supply_name_raw,
    v.wbs_name                                      AS supply_package,
    v.status                                        AS supply_status,
    COALESCE(v.actual_finish, v.early_finish)::date AS delivery_date,
    v.total_float_d                                 AS supply_float_d,
    da.calendar_key,
    cons.activity_id                                AS consumer_id,
    cons.activity_name                              AS consumer_name,
    cons.area                                       AS consumer_area,
    COALESCE(cons.actual_start, cons.early_start)::date AS required_on_site,
    cons.is_driving_path                            AS consumer_on_driving_path
FROM mart.v_activity v
JOIN core.dim_wbs w
      ON w.wbs_key = v.wbs_key AND w.phase = 'Procurement'
LEFT JOIN core.dim_activity da ON da.activity_key = v.activity_key
LEFT JOIN core.fact_relationship_snapshot r
      ON r.snapshot_key = v.snapshot_key
     AND r.pred_activity_key = v.activity_key
LEFT JOIN mart.v_activity cons
      ON cons.snapshot_key = v.snapshot_key
     AND cons.activity_key = r.succ_activity_key
WHERE v.snapshot_type <> 'BASELINE';
 
-- One row per procurement item per snapshot ------------------------------
-- The binding consumer is the EARLIEST one: a delivery is late the moment
-- it misses the first activity that needs it, not the average.
CREATE OR REPLACE VIEW mart.v_procurement_buffer AS
SELECT
    project_key,
    snap_date,
    supply_id,
    supply_name,
    supply_package,
    supply_status,
    delivery_date,
    supply_float_d,
    MIN(required_on_site)                                   AS required_on_site,
    MIN(required_on_site) - delivery_date                   AS buffer_cal_days,
    -- working days is the number that matters: a 24-day buffer spanning
    -- Diwali and four Sundays is not 24 days of recoverable time
    mart.workdays_between(MIN(calendar_key), delivery_date,
                          MIN(required_on_site))            AS buffer_work_days,
    COUNT(consumer_id)                                      AS consumer_count,
    BOOL_OR(consumer_on_driving_path)                       AS feeds_driving_path,
    CASE
      WHEN COUNT(consumer_id) = 0                            THEN 'UNLINKED'
      WHEN MIN(required_on_site) - delivery_date <  0        THEN 'LATE'
      WHEN MIN(required_on_site) - delivery_date <= 7        THEN 'CRITICAL'
      WHEN MIN(required_on_site) - delivery_date <= 21       THEN 'AT_RISK'
      ELSE 'OK'
    END                                                     AS buffer_status
FROM mart.v_procurement_link
GROUP BY project_key, snap_date, supply_id, supply_name, supply_package,
         supply_status, delivery_date, supply_float_d;
 
COMMENT ON VIEW mart.v_procurement_buffer IS
  'UNLINKED is a schedule defect, not a healthy buffer: a delivery with no
   successor floats to wherever the CPM puts it and can be scheduled after
   the work that needs it. Treat it as worse than LATE.';
 
-- Week-on-week buffer movement -------------------------------------------
-- Erosion is the signal. A 40-day buffer losing 6 days a week is in more
-- trouble than a stable 15-day buffer, and only a snapshot series shows it.
CREATE OR REPLACE VIEW mart.v_procurement_buffer_trend AS
SELECT
    project_key,
    supply_id,
    supply_name,
    supply_package,
    snap_date,
    buffer_cal_days,
    buffer_work_days,
    buffer_status,
    feeds_driving_path,
    LAG(buffer_work_days) OVER (PARTITION BY supply_id ORDER BY snap_date)
                                                            AS prev_buffer_work_days,
    buffer_work_days
      - LAG(buffer_work_days) OVER (PARTITION BY supply_id ORDER BY snap_date)
                                                            AS buffer_change_d,
    FIRST_VALUE(buffer_work_days) OVER (PARTITION BY supply_id ORDER BY snap_date)
                                                            AS first_buffer_work_days,
    buffer_work_days
      - FIRST_VALUE(buffer_work_days) OVER (PARTITION BY supply_id ORDER BY snap_date)
                                                            AS buffer_change_total_d
FROM mart.v_procurement_buffer
WHERE supply_status <> 'COMPLETE';
 
-- Watch list: what a procurement manager should act on this week ----------
CREATE OR REPLACE VIEW mart.v_procurement_watchlist AS
WITH latest AS (
    SELECT DISTINCT ON (project_key) project_key, snap_date
    FROM mart.v_procurement_buffer ORDER BY project_key, snap_date DESC
)
SELECT t.project_key, t.snap_date, t.supply_id, t.supply_name, t.supply_package,
       t.buffer_cal_days, t.buffer_work_days, t.buffer_change_d, t.buffer_change_total_d,
       t.buffer_status, t.feeds_driving_path,
       CASE
         WHEN t.buffer_status = 'UNLINKED'                        THEN 1
         WHEN t.buffer_status = 'LATE'                            THEN 2
         WHEN t.buffer_status = 'CRITICAL'                        THEN 3
         WHEN t.buffer_change_total_d < -14                       THEN 4
         WHEN t.buffer_status = 'AT_RISK'                         THEN 5
         ELSE 9
       END AS priority
FROM mart.v_procurement_buffer_trend t
JOIN latest l ON l.project_key = t.project_key AND l.snap_date = t.snap_date
WHERE t.buffer_status <> 'OK' OR t.buffer_change_total_d < -14
ORDER BY priority, t.buffer_work_days;
   

-- =====================================================================
-- OPERATIONAL VIEWS
-- The outputs a planner actually circulates, as opposed to the
-- analytical layer a manager reads. Run after 02_mart.sql.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 3-WEEK LOOK-AHEAD
-- The single most-used output in project controls: what starts and what
-- finishes in the next three weeks, from the CURRENT data date.
-- Driving-path activities first, then least float.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_lookahead AS
WITH cur AS (
    SELECT project_key, snapshot_key, data_date
    FROM mart.v_current_snapshot
)
SELECT v.project_key, c.data_date AS as_of,
       v.activity_id, v.activity_name, v.area, v.phase, v.wbs_path,
       v.status,
       COALESCE(v.actual_start,  v.early_start)::date  AS start_date,
       COALESCE(v.actual_finish, v.early_finish)::date AS finish_date,
       v.remaining_duration_d,
       v.total_float_d,
       v.is_driving_path,
       v.budget_value,
       CASE
         WHEN v.status = 'IN_PROGRESS'                                   THEN 'IN PROGRESS'
         WHEN v.early_start::date  <= c.data_date + 21                   THEN 'STARTING'
         ELSE 'FINISHING'
       END AS lookahead_type
FROM mart.v_activity v
JOIN cur c ON c.snapshot_key = v.snapshot_key
WHERE v.status <> 'COMPLETE'
  AND (   v.early_start::date  BETWEEN c.data_date AND c.data_date + 21
       OR v.early_finish::date BETWEEN c.data_date AND c.data_date + 21
       OR v.status = 'IN_PROGRESS');
COMMENT ON VIEW mart.v_lookahead IS
  'Three weeks forward from the latest data date. Sort by is_driving_path DESC,
   total_float_d ASC to get the order a planner would work through.';


-- ---------------------------------------------------------------------
-- CRITICAL PATH LISTING
-- is_driving_path is stored but nothing listed the path in sequence.
-- Uses driving path, not float: the float-based definition depends on a
-- Schedule Options setting, the driving path flag does not.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_critical_path AS
SELECT v.project_key, v.snap_date,
       ROW_NUMBER() OVER (PARTITION BY v.project_key, v.snap_date
                          ORDER BY COALESCE(v.actual_start, v.early_start)) AS path_seq,
       v.activity_id, v.activity_name, v.area, v.phase,
       v.status,
       COALESCE(v.actual_start,  v.early_start)::date  AS start_date,
       COALESCE(v.actual_finish, v.early_finish)::date AS finish_date,
       v.original_duration_d, v.remaining_duration_d,
       v.total_float_d, v.budget_value
FROM mart.v_activity v
WHERE v.is_driving_path
  AND v.snapshot_type <> 'BASELINE';

select * from mart.v_critical_path;
-- ---------------------------------------------------------------------
-- WEEKLY PROGRESS SUMMARY
-- What changed between one snapshot and the next. Turns a snapshot series
-- into a "what happened this week" narrative, which is what a weekly
-- report is actually asking for.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_weekly_progress AS
WITH pairs AS (
    SELECT s.project_key, s.snapshot_key, s.data_date,
           LAG(s.snapshot_key) OVER (PARTITION BY s.project_key ORDER BY s.snapshot_seq) AS prev_key,
           LAG(s.data_date)    OVER (PARTITION BY s.project_key ORDER BY s.snapshot_seq) AS prev_date
    FROM core.dim_snapshot s
), joined AS (
    SELECT p.project_key, p.data_date, p.prev_date,
           a.activity_id,
           cur.status        AS status_now,
           prv.status        AS status_before,
           cur.earned_value  AS ev_now,
           COALESCE(prv.earned_value, 0) AS ev_before,
           cur.is_driving_path
    FROM pairs p
    JOIN core.fact_activity_snapshot cur ON cur.snapshot_key = p.snapshot_key
    JOIN core.dim_activity a             ON a.activity_key   = cur.activity_key
    LEFT JOIN core.fact_activity_snapshot prv
           ON prv.snapshot_key = p.prev_key AND prv.activity_key = cur.activity_key
    WHERE p.prev_key IS NOT NULL
)
SELECT project_key, prev_date AS period_from, data_date AS period_to,
       COUNT(*) FILTER (WHERE status_now='COMPLETE' AND status_before<>'COMPLETE')        AS activities_finished,
       COUNT(*) FILTER (WHERE status_now='IN_PROGRESS' AND status_before='NOT_STARTED')   AS activities_started,
       COUNT(*) FILTER (WHERE status_now='IN_PROGRESS')                                   AS activities_in_progress,
       COUNT(*) FILTER (WHERE status_now='COMPLETE')                                      AS activities_complete_total,
       SUM(ev_now - ev_before)                                                            AS value_earned,
       SUM(ev_now - ev_before) FILTER (WHERE is_driving_path)                             AS value_earned_on_critical_path
FROM joined
GROUP BY project_key, prev_date, data_date;

-- ---------------------------------------------------------------------
-- SLAB CYCLE ANALYSIS
-- Floor-to-floor concrete cycle per tower, measured from actual pour
-- dates. On an aluminium formwork job this is THE production metric --
-- the number a construction director recognises instantly -- and it is
-- the only one here that measures physical output rather than schedule
-- position.
--
-- Uses the LATEST snapshot: actual dates never change once written, so
-- the newest snapshot holds the complete as-built pour record.
-- ---------------------------------------------------------------------
select * from mart.v_slab_cycle;

CREATE OR REPLACE VIEW mart.v_slab_cycle AS
WITH pours AS (
    SELECT v.project_key, v.area AS tower,
           v.activity_id, v.wbs_name AS level_name,
           v.actual_finish::date AS pour_date,
           da.calendar_key
    FROM mart.v_activity v
    JOIN mart.v_current_snapshot c ON c.snapshot_key = v.snapshot_key
    LEFT JOIN core.dim_activity da ON da.activity_key = v.activity_key
    WHERE v.activity_name = 'Concrete'
      AND v.phase   = 'Structure'
      AND v.area    IS NOT NULL
      AND v.actual_finish IS NOT NULL
), seq AS (
    SELECT p.*,
           LAG(pour_date)  OVER (PARTITION BY project_key, tower ORDER BY pour_date) AS prev_pour_date,
           LAG(activity_id) OVER (PARTITION BY project_key, tower ORDER BY pour_date) AS prev_activity_id,
           ROW_NUMBER()    OVER (PARTITION BY project_key, tower ORDER BY pour_date)  AS pour_seq
    FROM pours p
)
SELECT project_key, tower, pour_seq, activity_id, level_name, pour_date,
       prev_activity_id, prev_pour_date,
       (pour_date - prev_pour_date)                                        AS cycle_cal_days,
       mart.workdays_between(calendar_key, prev_pour_date, pour_date)      AS cycle_work_days,
       ROUND(AVG(pour_date - prev_pour_date)
             OVER (PARTITION BY project_key, tower), 1)                    AS tower_avg_cycle_cal_days
FROM seq
WHERE prev_pour_date IS NOT NULL and level_name <> 'Columns / shear walls';
COMMENT ON VIEW mart.v_slab_cycle IS
  'Aluminium formwork benchmark is 7-10 days floor to floor. This project runs
   13-15 days median. A cycle far above the tower median marks a specific
   disruption rather than general underperformance.';

SELECT snap_date, ROUND(bac/1e7,2) AS bac_cr,
       ROUND(pv/1e7,2) AS pv_cr, ROUND(ev/1e7,2) AS ev_cr,
       ROUND(pct_complete*100,1) AS pct, spi
FROM mart.v_evm_project ORDER BY snap_date;   

SELECT activity_id, activity_name, slip_cal_days, slip_work_days
FROM mart.v_milestone_trend
WHERE snap_date = '2025-06-27'
ORDER BY slip_cal_days DESC NULLS LAST LIMIT 8;


DROP VIEW IF EXISTS mart.v_procurement_watchlist;
DROP VIEW IF EXISTS mart.v_procurement_buffer_trend;
DROP VIEW IF EXISTS mart.v_procurement_buffer;
DROP VIEW IF EXISTS mart.v_procurement_link;
 
