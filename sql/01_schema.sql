CREATE SCHEMA IF NOT EXISTS landing;   -- raw parsed tables, XER-shaped
CREATE SCHEMA IF NOT EXISTS core;      -- canonical model, source-agnostic
CREATE SCHEMA IF NOT EXISTS mart;      -- analytical views

-- ---------------------------------------------------------------------
-- DIMENSIONS
-- ---------------------------------------------------------------------

CREATE TABLE core.dim_project (
    project_key         SERIAL PRIMARY KEY,
    project_id          TEXT NOT NULL UNIQUE,     
    project_name        TEXT NOT NULL,
    client_name         TEXT,
    currency_code       CHAR(3) NOT NULL DEFAULT 'INR',
    value_basis         TEXT NOT NULL             
        CHECK (value_basis IN ('currency','man_hours','man_days')),
    planned_start       DATE,
    planned_finish      DATE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON COLUMN core.dim_project.value_basis IS
  'APX loads rupees into budgeted units at Price/Unit=1, so value_basis=currency.
   A project loading genuine man-hours reads the same column differently.
   Never assume — every value query must respect this.';


CREATE TABLE core.dim_baseline (
    baseline_key        SERIAL PRIMARY KEY,
    project_key         INT  NOT NULL REFERENCES core.dim_project,
    baseline_code       TEXT NOT NULL,            
    baseline_type       TEXT,                     
    approved_on         DATE,
    effective_from      DATE NOT NULL,           
    effective_to        DATE,                     
    is_original         BOOLEAN NOT NULL DEFAULT FALSE,
    notes               TEXT,
    UNIQUE (project_key, baseline_code)
);
COMMENT ON COLUMN core.dim_baseline.is_original IS
  'Exactly one per project. Claims analysis always needs variance against the
   ORIGINAL baseline regardless of how many re-baselines have occurred since.';


CREATE TABLE core.dim_snapshot (
    snapshot_key        SERIAL PRIMARY KEY,
    project_key         INT  NOT NULL REFERENCES core.dim_project,
    baseline_key        INT  REFERENCES core.dim_baseline,   
    data_date           DATE NOT NULL,
    snapshot_type       TEXT NOT NULL
        CHECK (snapshot_type IN ('BASELINE','UPDATE','REBASELINE','WHAT_IF')),
    snapshot_seq        INT  NOT NULL,            
    source_system       TEXT NOT NULL DEFAULT 'P6',
    source_format       TEXT NOT NULL DEFAULT 'XER',
    source_file         TEXT NOT NULL,
    file_sha256         CHAR(64) NOT NULL,
    retained_logic      BOOLEAN,
    critical_path_type  TEXT,
    default_pct_type    TEXT,
    project_finish      TIMESTAMP,
    must_finish_by      TIMESTAMP,
    loaded_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_key, data_date, snapshot_type),
    UNIQUE (project_key, file_sha256)
);

CREATE TABLE core.dim_calendar_day (
    calendar_key        INT  NOT NULL,
    project_key         INT  NOT NULL REFERENCES core.dim_project,
    calendar_name       TEXT NOT NULL,
    cal_date            DATE NOT NULL,
    working_hours       NUMERIC(6,2) NOT NULL DEFAULT 0,
    is_working_day      BOOLEAN NOT NULL,
    is_exception        BOOLEAN NOT NULL DEFAULT FALSE,
    workday_index       INT NOT NULL,
    PRIMARY KEY (calendar_key, cal_date)
);

CREATE TABLE core.dim_date (
    date_key            DATE PRIMARY KEY,
    year                INT NOT NULL,
    quarter             INT NOT NULL,
    month               INT NOT NULL,
    month_name          TEXT NOT NULL,
    week_of_year        INT NOT NULL,
    iso_year_week       TEXT NOT NULL,
    day_of_week         INT NOT NULL,
    is_week_end         BOOLEAN NOT NULL,     -- Friday: the reporting cut-off
    fiscal_year         INT NOT NULL,         -- Indian FY, April start
    fiscal_quarter      INT NOT NULL
);

CREATE TABLE core.dim_wbs (
    wbs_key             SERIAL PRIMARY KEY,
    project_key         INT  NOT NULL REFERENCES core.dim_project,
    wbs_id              TEXT NOT NULL,            -- natural key within project
    wbs_code            TEXT NOT NULL,
    wbs_name            TEXT NOT NULL,
    parent_wbs_id       TEXT,
    wbs_level           INT  NOT NULL,
    wbs_path            TEXT NOT NULL,
    area                TEXT,                     -- 'Tower 1', 'Club House'
    phase               TEXT,                     -- 'Structure', 'Finishes', 'T&C'
    valid_from          DATE NOT NULL,
    valid_to            DATE,
    is_current          BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (project_key, wbs_id, valid_from)
);


CREATE TABLE core.dim_activity (
    activity_key        SERIAL PRIMARY KEY,
    project_key         INT  NOT NULL REFERENCES core.dim_project,
    activity_id         TEXT NOT NULL,            -- natural key, e.g. 'PG-10091'
    activity_name       TEXT NOT NULL,
    activity_type       TEXT NOT NULL,
    duration_type       TEXT,
    pct_complete_type   TEXT,
    calendar_key        INT,
    wbs_id              TEXT NOT NULL,
    is_milestone        BOOLEAN NOT NULL DEFAULT FALSE,
    is_loe              BOOLEAN NOT NULL DEFAULT FALSE,
    valid_from          DATE NOT NULL,
    valid_to            DATE,
    is_current          BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (project_key, activity_id, valid_from)
);

CREATE TABLE core.dim_resource (
    resource_key        SERIAL PRIMARY KEY,
    project_key         INT  NOT NULL REFERENCES core.dim_project,
    resource_id         TEXT NOT NULL,
    resource_name       TEXT NOT NULL,
    resource_type       TEXT,                     -- Labor / Nonlabor / Material
    unit_of_measure     TEXT,
    price_per_unit      NUMERIC(18,4),
    contractor_name     TEXT,
    trade               TEXT,
    UNIQUE (project_key, resource_id)
);

-- ---------------------------------------------------------------------
-- FACTS 
-- ---------------------------------------------------------------------

-- Grain: one row per activity per snapshot.
CREATE TABLE core.fact_activity_snapshot (
    snapshot_key        INT  NOT NULL REFERENCES core.dim_snapshot,
    activity_key        INT  NOT NULL REFERENCES core.dim_activity,
    wbs_key             INT  NOT NULL REFERENCES core.dim_wbs,
    project_key         INT  NOT NULL REFERENCES core.dim_project,
    data_date           DATE NOT NULL,
    status              TEXT NOT NULL
        CHECK (status IN ('NOT_STARTED','IN_PROGRESS','COMPLETE')),
    original_duration_d NUMERIC(10,2),
    remaining_duration_d NUMERIC(10,2),
    actual_duration_d   NUMERIC(10,2),
    at_completion_dur_d NUMERIC(10,2),
    early_start         TIMESTAMP,
    early_finish        TIMESTAMP,
    late_start          TIMESTAMP,
    late_finish         TIMESTAMP,
    actual_start        TIMESTAMP,
    actual_finish       TIMESTAMP,
    planned_start       TIMESTAMP,
    planned_finish      TIMESTAMP,
    total_float_d       NUMERIC(10,2),
    free_float_d        NUMERIC(10,2),
    is_critical         BOOLEAN,
    is_driving_path     BOOLEAN,
    constraint_type     TEXT,
    constraint_date     TIMESTAMP,
    pct_complete        NUMERIC(6,3),             -- 0..1
    budget_value        NUMERIC(18,2),            -- BAC in value_basis units
    earned_value        NUMERIC(18,2),            -- pct_complete * budget
    PRIMARY KEY (snapshot_key, activity_key)
);

CREATE TABLE core.fact_relationship_snapshot (
    snapshot_key        INT  NOT NULL REFERENCES core.dim_snapshot,
    pred_activity_key   INT  NOT NULL REFERENCES core.dim_activity,
    succ_activity_key   INT  NOT NULL REFERENCES core.dim_activity,
    project_key         INT  NOT NULL REFERENCES core.dim_project,
    data_date           DATE NOT NULL,
    relationship_type   TEXT NOT NULL CHECK (relationship_type IN ('FS','SS','FF','SF')),
    lag_d               NUMERIC(10,2) NOT NULL DEFAULT 0,
    is_driving          BOOLEAN,
    PRIMARY KEY (snapshot_key, pred_activity_key, succ_activity_key, relationship_type)
);

CREATE TABLE core.fact_assignment_snapshot (
    snapshot_key        INT  NOT NULL REFERENCES core.dim_snapshot,
    activity_key        INT  NOT NULL REFERENCES core.dim_activity,
    resource_key        INT  NOT NULL REFERENCES core.dim_resource,
    project_key         INT  NOT NULL REFERENCES core.dim_project,
    data_date           DATE NOT NULL,
    budgeted_units      NUMERIC(18,2),
    actual_units        NUMERIC(18,2),
    remaining_units     NUMERIC(18,2),
    budgeted_cost       NUMERIC(18,2),
    actual_cost         NUMERIC(18,2),
    remaining_cost      NUMERIC(18,2),
    PRIMARY KEY (snapshot_key, activity_key, resource_key)
);

CREATE TABLE core.fact_actual_cost (
    cost_key            BIGSERIAL PRIMARY KEY,
    project_key         INT  NOT NULL REFERENCES core.dim_project,
    activity_key        INT  NOT NULL REFERENCES core.dim_activity,
    resource_key        INT  REFERENCES core.dim_resource,
    period_end          DATE NOT NULL,            -- month end
    cost_type           TEXT NOT NULL
        CHECK (cost_type IN ('LABOUR','MATERIAL','SUBCONTRACT','PLANT')),
    actual_cost         NUMERIC(18,2) NOT NULL,
    source_system       TEXT NOT NULL DEFAULT 'ERP',
    source_document     TEXT,                     -- invoice / certificate ref
    loaded_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_key, activity_key, period_end, cost_type, resource_key)
);

-- ---------------------------------------------------------------------
-- AUDIT
-- ---------------------------------------------------------------------

CREATE TABLE core.fact_load_audit (
    load_key            BIGSERIAL PRIMARY KEY,
    project_key         INT REFERENCES core.dim_project,
    snapshot_key        INT REFERENCES core.dim_snapshot,
    source_file         TEXT NOT NULL,
    file_sha256         CHAR(64) NOT NULL,
    data_date           DATE,
    status              TEXT NOT NULL
        CHECK (status IN ('LOADED','SKIPPED_DUPLICATE','REJECTED','FAILED')),
    rows_activities     INT,
    rows_relationships  INT,
    rows_assignments    INT,
    checks_passed       INT,
    checks_failed       INT,
    reject_reason       TEXT,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ
);

CREATE TABLE core.fact_validation_result (
    load_key            BIGINT NOT NULL REFERENCES core.fact_load_audit,
    rule_code           TEXT NOT NULL,
    severity            TEXT NOT NULL CHECK (severity IN ('ERROR','WARNING','INFO')),
    affected_rows       INT NOT NULL,
    sample_keys         TEXT,
    message             TEXT
);

-- ---------------------------------------------------------------------
-- INDEXES
-- ---------------------------------------------------------------------
CREATE INDEX ix_fas_activity   ON core.fact_activity_snapshot (activity_key, data_date);
CREATE INDEX ix_fas_datadate   ON core.fact_activity_snapshot (project_key, data_date);
CREATE INDEX ix_fas_wbs        ON core.fact_activity_snapshot (wbs_key, data_date);
CREATE INDEX ix_fas_driving    ON core.fact_activity_snapshot (snapshot_key) WHERE is_driving_path;
CREATE INDEX ix_frs_pred       ON core.fact_relationship_snapshot (pred_activity_key, data_date);
CREATE INDEX ix_frs_succ       ON core.fact_relationship_snapshot (succ_activity_key, data_date);
CREATE INDEX ix_fasg_res       ON core.fact_assignment_snapshot (resource_key, data_date);
CREATE INDEX ix_fac_period     ON core.fact_actual_cost (project_key, period_end);
CREATE INDEX ix_dim_act_cur    ON core.dim_activity (project_key, activity_id) WHERE is_current;
CREATE INDEX ix_dim_wbs_cur    ON core.dim_wbs (project_key, wbs_id) WHERE is_current;
CREATE INDEX ix_cal_day        ON core.dim_calendar_day (calendar_key, cal_date);
