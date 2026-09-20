# P6 and Schedule Warehouse — Lessons Log

Project: APX (Prestige Park Grove) — 3,087 activities, 16 snapshots, P6 Professional 25.12
Living document. Append as new issues are found.

Format per entry: **Symptom → Diagnosis → Fix → How it was found.**

---

# PART 1 — P6 DATA MODEL

## 1.1 Duration Type `Fixed Units/Time` lets units drive duration

**Symptom.** 1,317 activities with Original Durations over a year. Longest 55,701 days. Planned finishes in 2203.

**Diagnosis.** Cost had been loaded into the Budgeted Units field (₹1,00,000 entered as 100,000 units) while Duration Type was `Fixed Units/Time`. P6 then computes:

```
Duration = Units ÷ Units-per-Time = 100,000 ÷ 1 = 100,000 hr ÷ 12 = 8,333 days
```

**Fix.** Set Duration Type to **Fixed Duration & Units** on every activity. Duration then cannot be touched by units.

The four types, and why only one is right for construction:

| Type | Fixed | Use here? |
|---|---|---|
| Fixed Duration & Units | duration + total units | **yes** |
| Fixed Duration & Units/Time | duration + rate; recalculates units | no — silently rewrites budgeted units |
| Fixed Units | units; duration = units ÷ rate | never |
| Fixed Units/Time | rate; duration = units ÷ rate | never — this was the bug |

**Found by.** Sorting Original Duration descending.

---

## 1.2 Remaining Duration = 0 collapses the CPM and hides the first problem

**Symptom.** Project finish looked plausible (2026-05-18) despite planned finishes in 2203.

**Diagnosis.** 1,549 activities had `remain_drtn_hr_cnt = 0` while Original Duration was non-zero and status was Not Started. **F9 schedules from Remaining Duration, not Original.** So the CPM treated half the network as zero-length point events and the whole schedule was driven by relationship lags alone.

**Fix.** Remaining Duration must equal Original Duration on any Not Started activity. Import both columns with the same value.

**Found by.**
```sql
-- activities that cannot legitimately exist
SELECT count(*) FROM tasks
WHERE status = 'Not Started' AND remaining_duration = 0 AND original_duration > 0;
```

---

## 1.3 Planned Start / Planned Finish are CPM outputs, not inputs

**Symptom.** Could not type corrected dates into Planned Start / Planned Finish.

**Diagnosis.** They are calculated from duration, logic and calendar. P6 will not accept typed values, and Excel export marks them `(*)` read-only.

**Fix.** Fix the duration or the logic, then F9. The dates correct themselves.

**Lesson.** In P6, duration is the input and dates are the output. This is the reverse of a spreadsheet.

---

## 1.4 Planned Finish and Early Finish diverge when OD ≠ RD

**Symptom.** 283 Not Started activities where `early_finish != planned_finish`, by a few hours.

**Diagnosis.** P6 does mirror Planned to Early for Not Started activities, but the two use **different duration fields**:

```
Early Finish   = Early Start   + Remaining Duration
Planned Finish = Planned Start + Original Duration
```

Equal when OD = RD. Different otherwise. The 283 mismatches were exactly the 283 activities where OD ≠ RD — perfect overlap, zero exceptions.

**Fix.** None needed. All differed by under half a day, and nothing downstream reads the planned dates.

**Found by.**
```sql
SELECT count(*) FROM core.fact_activity_snapshot
WHERE snapshot_key = 1 AND early_finish <> planned_finish;
-- then confirm the set equals the OD != RD set
```

---

## 1.5 FF relationships constrain the successor's FINISH

**Symptom.** A forward-pass simulation produced 828 days of slip on a 3-year project.

**Diagnosis.** Finish-to-Finish was being treated as a start constraint. With 1,379 FF links, each successor was pushed forward by a full activity length.

**Fix.** For FF and SF, compute the finish constraint and back-calculate the start through the duration:

```
FS: start  >= pred_finish + lag
SS: start  >= pred_start  + lag
FF: finish >= pred_finish + lag   ->  start = finish - duration
SF: finish >= pred_start  + lag   ->  start = finish - duration
```

After the fix, total slip came out at ~9 days.

---

## 1.6 Zero-duration milestones break naive calendar arithmetic

**Symptom.** Calendar validation matched 3,076 of 3,087 activities. The 11 failures were all milestones.

**Diagnosis.** `add_hours(start, 0)` was snapping forward to the next shift start. P6 keeps a milestone at the instant it sits on — often 18:00, the end of the working day.

**Fix.**
```python
if hours <= 1e-9:
    return start      # zero duration: no forward snap
```

**Lesson.** Validate to 100%, not 99%. The last 1% is always a real edge case.

---

## 1.7 Level of Effort activities distort every metric

Not present in this project, but the reason `is_loe` exists.

An LOE takes its duration from the activities it spans (supervision, prelims, QA). It carries budget but earns purely by elapsed time, so it **cannot be late** — including LOEs drags SPI toward 1.0, inflates open-end counts, and can appear critical while driving nothing.

Exclude with `WHERE NOT is_loe`.

---

# PART 2 — P6 EXCEL IMPORT MECHANICS

The single most costly area. Every one of these was discovered by a failed import.

## 2.1 `(*)` prefix means read-only — silently ignored

**Symptom.** Imported values simply did not appear. No error.

**Diagnosis.** P6's Excel export marks read-only fields with `(*)` in row 2. On import they are discarded without comment.

Read-only in the Activities export: `Start`, `Finish`, `Planned Start`, `Planned Finish`, `Predecessors`, `Successors`, `Resources`, `Duration Type`, `Activity Status`, `Resource Type`, `Role ID`, `WBS Name`, `WBS Path`, `WBS Category`, `Price / Unit`.

**Lesson.** Check row 2 for `(*)` before building any import file.

---

## 2.2 The importer has a whitelist — valid field names are not enough

**Symptom.** `Physical % Complete` and `Actual Regular Units` imported with no error and no effect. All 63 in-progress activities read 0%; all 3,045 assignments read 0 actual units.

**Diagnosis.** P6's spreadsheet importer accepts a fixed set of fields. A correct XER field name that is not on the list is dropped silently.

Confirmed importable: `Activity ID`, `Activity Name`, `WBS Code`, `Actual Start`, `Actual Finish`, `Original Duration`, `Remaining Duration`, `% Complete Type`, `Constraint Type/Date`, `Delete This Row`, and relationships (`Predecessor`, `Successor`, `Relationship Type`, `Lag`).

Confirmed **not** importable: `Physical % Complete`, `Actual Units`, `Price / Unit`, `Activity Type`, anything marked `(*)`.

**Fix.** Drive progress through **Remaining Duration** and set `% Complete Type = Duration`. P6 then computes:

```
% Complete = (Original − Remaining) ÷ Original
```

Remaining Duration *is* importable, so this works where Physical % does not.

**Lesson.** Never assume an import worked because the log was clean. Verify the values landed.

---

## 2.3 Omitting `wbs_id` relocates every row to the project root

**Symptom.** 962 activities appeared directly under the project node, out of the WBS.

**Diagnosis.** A TASK import without a `wbs_id` column. P6 reads the blank as "move to root", not "leave unchanged". Cost two separate incidents.

**Fix.** **Always include `wbs_id` in any TASK import.** Source it from a known-good export, never reconstruct it while the live project is damaged — the damaged export carries `APX` for the parked rows and would bake the problem in permanently.

**Found by.**
```sql
SELECT count(*) FROM activities WHERE wbs_code = 'APX';   -- project root
```

---

## 2.4 Column order matters: Actual Finish before Actual Start

**Symptom.** 133 warnings, all Tower 2 and Tower 3 short structural activities:
> Activity Remain Finish Date cannot be earlier than Actual Start Date

**Diagnosis.** P6 processes columns left to right. With `Activity Status` first and `Actual Start` before `Actual Finish`, P6 marks the activity Complete, stamps a provisional finish from the baseline date, then rejects the actual start as later than it. Only activities short enough for the slip to exceed their own duration were affected.

**Fix.**
- Put `act_end_date` **before** `act_start_date`, matching P6's own export order.
- **Omit `status_code` entirely.** P6 derives status from the dates: start + finish = Complete, start only = In Progress.

---

## 2.5 Advance the data date BEFORE importing actuals

**Symptom.** 52 rows rejected with the same remain-finish error.

**Diagnosis.** P6 computes `Remaining Start` from the current data date. If an actual start is later than the data date, remaining work would begin before the activity started — which is impossible, so P6 refuses.

**Fix.** Order is always:
```
1. Set data date to the file's date
2. Import
3. F9
4. Export
```

Some warnings still appear for activities starting inside the update window; they resolve on F9. Rejections do not. **Check the resulting counts, not the log.**

---

## 2.6 Dates must be real datetime cells with P6's number format

**Symptom.** Dates misparsed or displayed as serial numbers.

**Diagnosis.** Strings written as `2024-03-11 17:00` are ambiguous under a `dd/mm/yyyy` locale. P6 writes real datetime values with number format `dd-MM-yyyy HH:mm`.

**Fix.** Write datetime objects and set the format on **every** date cell, not just the ones you edited.

---

## 2.7 Unit of Measure is a lookup, not free text

**Symptom.** `WARNING: RSRC: Failed to resolve foreign key for field Unit Name with value INR. Field cleared.`

**Fix.** Add the value in `Admin → Admin Categories → Units of Measure` first.

---

## 2.8 Activity Type is read-only — milestones import as Tasks

**Symptom.** 42 imported milestones came in as `TT_Task` with zero duration.

**Fix.** Set Activity Type in the P6 UI: select the rows, set the first, Fill Down.

---

## 2.9 Price / Unit cannot be exported or imported

**Symptom.** Budgeted Cost stayed ₹0 while Budgeted Units read correctly.

**Fix.** `Enterprise → Resources → Units & Prices → Price/Unit`. UI only.

Setting it to **1** with UoM = INR makes units and cost identical, which is how a lump-sum-per-activity model is loaded.

---

## 2.10 Full-state files beat delta files

Every weekly update file carried the **complete** state at its data date, not just the changes.

Benefits: re-importing is safe and idempotent; a failed import is fixed by running the same file again; nothing to unwind.

---

## 2.11 Always keep `USERDATA` and use `D` to delete

The `USERDATA` sheet carries settings P6 needs (`DurationQtyType`, `DateFormat`). Preserve it in every generated file.

`Delete This Row` = `D`.

---
## 2.12 A failed import can leave a fix half-applied

Symptom. 283 Not Started activities where early_finish != planned_finish, discovered in the loaded warehouse months after the import that was supposed to fix them.

Diagnosis — a three-step chain.

The original problem: 286 activities had Original Duration and Remaining Duration differing by a sub-day amount (OD 4.5d, RD 5.0d). Legitimate P6 data carried over from the parent schedule, but an activity that hasn't started cannot have work remaining that differs from work planned.

The fix attempt: an import of Activity ID, Original Duration(d), Remaining Duration(d), both set to the same whole number.

That import failed — it omitted wbs_id, so P6 relocated 962 activities to the project root (see 2.3) and applied nothing else. Durations were never written.

The repair that followed carried only Activity ID and WBS Code, deliberately scoped to one change at a time. So the WBS was restored and the durations stayed unfixed.

The residue surfaced later as a date mismatch, because:

Early Finish   = Early Start   + Remaining Duration
Planned Finish = Planned Start + Original Duration

The 283 mismatched dates were exactly the 283 OD ≠ RD activities. Perfect overlap, zero exceptions either way — which is how the chain was confirmed rather than guessed.

Fix. None applied. All differ by under half a day, nothing downstream reads the planned dates, and the validator absorbs it with an explicit tolerance:

python
add("DUR_NOTSTART_RD_NE_OD", "WARNING",
    [a.activity_id for a in A
     if a.status == "NOT_STARTED"
     and abs((a.remaining_duration_d or 0) - (a.original_duration_d or 0)) > 0.5],
    "NOT_STARTED activity where remaining duration differs from original by >0.5d")

Found by.

sql
-- the symptom
SELECT count(*) FROM core.fact_activity_snapshot
WHERE snapshot_key = 1 AND early_finish <> planned_finish;   -- 283

-- the cause: confirm the two sets are identical
SELECT count(*) FROM core.fact_activity_snapshot
WHERE snapshot_key = 1
  AND abs(original_duration_d - remaining_duration_d) > 0.001;  -- 283

Lessons.

When an import fails, know exactly which changes landed and which didn't. The rollback was partial, and the gap went unnoticed for weeks.
A repair file should fix only the damage. Scoping the repair to WBS alone was right — but it meant the original fix had to be re-applied separately, and it never was.
A tolerance in a validator is a decision, not a default. It must be documented with the reason, or the next person treats the data as clean.


# PART 3 — SCHEDULING BEHAVIOUR

## 3.1 Adding relationships changes nothing until F9

**Symptom.** 25 relationships added, no dates moved.

**Diagnosis.** P6 stores the link and leaves dates alone until the scheduler runs.

**Lesson.** Structural edits are inert until F9. Always F9 and verify.

---

## 3.2 Check existing logic direction before adding a link

**Symptom.** Two circular relationship loops after a logic-fix session.

**Diagnosis.** A suggested link duplicated an existing one in the opposite direction, closing a ring.

**Fix.** Open the Relationships tab and check before adding. Delete the redundant link.

---

## 3.3 Baselines do not travel in the XER

**Symptom.** `base_proj_id`, `baselines_to_export` all empty.

**Diagnosis.** A P6 baseline is a **separate project record**. The standard XER export writes only the selected project.

**Fix.** Export the baseline as its own XER and treat it as `snapshot_type = 'BASELINE'`. P6's internal baseline object is then irrelevant to the warehouse.

---

## 3.4 Baseline AFTER the final F9, never before

A baseline is a frozen copy. Anything changed afterwards produces variance that came from your edit rather than from progress. Had to be deleted and recreated twice.

**Order:** all changes → F9 → verify → create baseline → **assign it as Project Baseline** (the step everyone forgets; without it every BL field stays empty) → export.

---

## 3.5 Extracting a subset of a larger schedule leaves disconnected chains

**Symptom.** Occupation Certificate scheduled January 2024, before anything was built. Transformer delivery July 2026, three months *after* transformer commissioning.

**Diagnosis.** When a subset is cut out of a bigger network, relationships pointing to activities outside the subset are dropped. Those chains go free-floating and the CPM puts them at the earliest date logic allows. Signature: **total float of 500–900 days on a 3-year project.**

**Fix.** 19 relationships added by hand. Six procurement deliveries had no successor at all — the delivery was not linked to the work that consumes it.

**Found by.**
```sql
-- open ends, and the float that reveals them
SELECT activity_id, total_float_d FROM ... WHERE total_float_d > 365 ORDER BY total_float_d DESC;
```

**Lesson.** A percentage of open ends is not enough. 0.6% sounded fine; the specific activities were the problem.

---

## 3.6 Schedule Options change numbers without changing work

Settings that silently alter every metric:

| Setting | Was | Changed to | Effect |
|---|---|---|---|
| Define critical activities as | Total Float ≤ 0 | **Longest Path** | which activities are critical |
| Compute Total Float as | Finish Float | **Smallest of Start and Finish Float** | every float number |
| Use Expected Finish Dates | ticked | **off** | would override remaining duration if ever set |
| Activity Type | Resource Dependent | **Task Dependent** | which calendar governs dates |

**Lesson.** Store these settings in the warehouse per snapshot. Changing one mid-series makes snapshots incomparable, and the resulting jump looks like a real event.

```sql
SELECT DISTINCT retained_logic, critical_path_type, default_pct_type
FROM core.dim_snapshot;   -- one row = comparable series
```

---

## 3.7 Resource Dependent schedules on the RESOURCE's calendar

Only matters when a resource calendar differs from the activity calendar. On this project all 3,087 activities sat on one calendar, so switching to Task Dependent moved no dates — but the exposure was real.

---

# PART 4 — PROGRESS AND EARNED VALUE

## 4.1 % Complete Type decides which field P6 reads

Same data, different door:

| Type | P6 reads |
|---|---|
| Physical | a number typed in (not importable via Excel) |
| Duration | computed: `(OD − RD) ÷ OD` |
| Units | computed from units consumed |

Switching from Physical to Duration made 63 activities jump from 0% to their true value **with no data change** — the Remaining Durations were already there.

---

## 4.2 Activity % Complete vs Schedule % Complete

- **Activity % Complete** — how much is done. Governed by % Complete Type.
- **Schedule % Complete** — how much *should* be done, from baseline dates vs the data date.

Their ratio is SPI at activity level:

```
SPI = Activity % Complete ÷ Schedule % Complete
```

---

## 4.3 Actual Duration vs Remaining Duration

- **Actual Duration** — calculated by P6: working time from Actual Start to the data date. Backward-looking, can only grow.
- **Remaining Duration** — a **judgement** entered weekly. Drives every forecast date.

`Actual + Remaining = At Completion Duration`. Compare to Original for duration variance.

There is **no actual-duration column in the XER** — `act_work_qty` is labour units, not time. It must be derived from the calendar.

---

## 4.4 Percent complete needs a floor at zero

An overrunning activity can carry RD > OD, making `(OD − RD) ÷ OD` negative. P6 clamps to 0.

```python
pct = max(0.0, min(1.0, (od - rd) / od))
```

---

## 4.5 Units and cost must be internally consistent

`Actual + Remaining = At Completion`. If Actual Units never imported, this identity fails — nothing is lost, one term is simply missing.

On this project EV is derived from **durations**, not units, so the missing actual units affect nothing. But P6's own unit-based EVM reports would be wrong.

---

## 4.6 Actual cost does not come from the scheduler

Actual spend lives in invoices, certificates and payroll. It belongs in a separate ERP-fed table.

Certification **lags** work by weeks — that lag is what makes CPI informative. If actual cost were derived from earned value, CPI would be 1.0 forever.

---

# PART 5 — XER FORMAT

## 5.1 Structure

Tab-delimited with line-type markers:

```
%T  TASK                      table starts
%F  task_id  task_code ...    column names
%R  12345    PG-10091  ...    one row
```

Encoding is `cp1252`. Some third-party tools write UTF-8 — try both.

## 5.2 Everything is a string; empty is `''`, not null

`float('')` raises. Casting must be explicit, and an empty value must become `None`, not `0`.

Critical case: a completed activity has **no float**. `total_float_hr_cnt` is `''`. Cast it to 0 and every finished activity looks critical.

## 5.3 Durations are hours; divide by the activity's own calendar

```
344 hr ÷ 12 h/day = 28.67 days
```

Divide by a constant and every activity on a different calendar is wrong.

## 5.4 `task_id` for joins, `task_code` for identity

`TASKPRED` and `TASKRSRC` reference the numeric `task_id`, which is internal to one P6 database and changes between exports. **Never store it as a key.** Translate to `task_code` immediately.

## 5.5 Fields are not where you expect

`critical_path_type` is in `PROJECT`, not `SCHEDOPTIONS`. Reading the wrong table with `.get()` silently returns `None` instead of failing.

**Lesson.** Verify every field name against the actual file. A `.get()` that never crashes is not the same as a `.get()` that works.

## 5.6 `clndr_data` encoding

```
(0||CalendarData()(
   (0||DaysOfWeek()( (0||<dow>()( (0||0(f|18:00|s|06:00)()) )) ... ))
   (0||Exceptions()( (0||0(d|45201)()) ... ))))
```

- `dow`: 1 = Sunday … 7 = Saturday
- `f` = shift finish, `s` = shift start
- exception serial = days since **1899-12-30** (Excel epoch)
- no shifts = non-working

## 5.7 Dates are timestamps, and activities start mid-day

Durations are fractional and activities start at arbitrary times because predecessors finish mid-shift. **Date arithmetic must be hour-level against shift boundaries**, not day-level. Day-level arithmetic matched only 33% of P6's finish dates; hour-level matched 100%.

## 5.8 P6 collapses CPM dates onto the data date once complete

A completed activity shows `early_start = early_finish = data date`. Always use `COALESCE(actual_finish, early_finish)`.

---

# PART 6 — WAREHOUSE DESIGN

## 6.1 The data date is the anchor of everything

Not on activity rows. Not exportable to xlsx. Lives in `PROJECT.last_recalc_date`.

Every metric is *as of* a data date — PV, Schedule % Complete, total float, remaining duration. Without it a snapshot cannot be interpreted.

**This single fact is why XER is the only viable ingestion format.** P6's spreadsheet export has no Project or WBS subject area, so it cannot carry the data date, the hierarchy, or the calendars.

## 6.2 Surrogate keys for storage, natural keys for trends

SCD2 dimensions mint a **new `activity_key`** when any attribute changes. Any view joining or partitioning on that surrogate breaks at the point of change:

- float series restarts
- baseline join fails, variance goes NULL
- budget drops out of earned value

All silently. **Trends must use `activity_id`.**

This was the most dangerous bug found — it produces a plausible dashboard with wrong numbers.

## 6.3 `workday_index` makes working-day arithmetic a subtraction

One row per calendar per date, including non-working days, with a running count that does not increment on days off.

```sql
workdays_between(cal, a, b) = idx(b) - idx(a)
```

Calendar-day variance **overstates slip by ~15%** on a 6-day week with 20 site holidays.

## 6.4 Relationships as a fact, not a dimension

Logic changes move the critical path with no change to any activity's dates. Snapshotting gives a change register — links added, removed, retyped, re-lagged.

"What logic changed last week" is the first question in a delay claim and most tools cannot answer it.

## 6.5 Reject the whole file, never load partially

A schedule loaded with 3,086 of 3,087 activities is worse than no schedule: nothing downstream can tell it is incomplete. One transaction per file, ERROR rolls back everything.

## 6.6 Hash the file for idempotency

SHA-256 over the bytes. Re-running skips and records `SKIPPED_DUPLICATE`. Filenames lie; content does not. Also a tamper record — you can prove which file produced a number.

## 6.7 Audit rejections, not just successes

Every attempt writes a row including failures. Silent failure is what destroys trust in a pipeline.

## 6.8 `value_basis` must be declared, never assumed

This project loads **rupees** into Budgeted Units at Price/Unit = 1. Another project loads genuine man-hours into the same field. Same column, different meaning. Declared per project in config.

## 6.9 Contractor comparison is only valid within a trade

Comparing a painting subcontractor to a civil one measures the work, not the contractor. Valid peer groups: same trade, or the same contractor across projects.

## 6.10 Generic pipeline, per-project config

Parser, calendar decoder, snapshot model, DCMA checks and EVM formulas are universal. WBS shape, coding conventions, calendars and resource meaning differ in every project.

Onboarding a new project = write a config file and map dimensions. Not a code change.

---

# PART 7 — TOOLING

## 7.1 pgAdmin breaks dollar-quoted function bodies

**Symptom.** `ERROR: unterminated dollar-quoted string`

**Diagnosis.** The Query Tool splits on semicolons and cuts the function body.

**Fix.** Use single-quoted bodies, or run the file with `psql -f`.

## 7.2 Static parsing beats assumption

Eleven bugs were found by cross-checking code against the schema and the canonical model rather than by running it:

- fields referenced that do not exist on any dataclass
- columns inserted that do not exist in the DDL
- hardcoded literals where a variable belongs (`is_loe` = `False`)
- stray sheets in generated workbooks that would have rewritten relationships

**Lesson.** A validator written by the same author as the generator shares its blind spots. Check with different code.

---

# APPENDIX — DIAGNOSTIC QUERIES

```sql
-- settings consistency: one row means the series is comparable
SELECT DISTINCT retained_logic, critical_path_type, default_pct_type
FROM core.dim_snapshot;

-- load audit
SELECT s.data_date, s.snapshot_type, a.status, a.rows_activities
FROM core.fact_load_audit a
JOIN core.dim_snapshot s ON s.snapshot_key = a.snapshot_key
ORDER BY s.data_date;

-- budget must be identical on every snapshot
SELECT data_date, SUM(budget_value) FROM core.fact_activity_snapshot
GROUP BY data_date ORDER BY data_date;

-- activities parked at the project root
SELECT count(*) FROM core.dim_wbs WHERE wbs_code = 'APX';

-- SCD2 churn: has any activity changed attributes?
SELECT activity_id, count(*) FROM core.dim_activity
GROUP BY activity_id HAVING count(*) > 1;

-- open ends
SELECT count(*) FROM core.fact_activity_snapshot f
WHERE NOT EXISTS (SELECT 1 FROM core.fact_relationship_snapshot r
                  WHERE r.snapshot_key=f.snapshot_key AND r.succ_activity_key=f.activity_key);

-- working vs calendar day arithmetic
SELECT mart.workdays_between(6604,'2025-08-13','2025-08-18') AS working,
       '2025-08-18'::date - '2025-08-13'::date               AS calendar;
```

---

# RUNNING TALLY

| Area | Issues logged |
|---|---|
| P6 data model | 7 |
| Excel import mechanics | 11 |
| Scheduling behaviour | 7 |
| Progress and EVM | 6 |
| XER format | 8 |
| Warehouse design | 10 |
| Tooling | 2 |
| **Total** | **51** |

*Next entries: Power BI modelling and DAX.*

8.2 A snapshot fact needs point-in-time measures, not aggregations. SUM across snapshots double-counts a value that never accumulated; AVERAGE blends unrelated states. Both return a plausible number. The correct pattern anchors to MAX(Date) in the current filter context, which evaluates to the latest snapshot on a card and to each snapshot in turn on a time axis.