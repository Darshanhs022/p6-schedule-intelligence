"""Contract validation. Runs BEFORE anything is written.

Any ERROR rejects the whole file. A schedule loaded with 3,086 of 3,087
activities is worse than no schedule, because nothing downstream can tell
it is incomplete. Warnings are recorded and loaded.

Rule codes are stable so trends in data quality can themselves be tracked.
"""
from collections import Counter, defaultdict
from dataclasses import dataclass


@dataclass
class Finding:
    rule_code: str
    severity: str          # ERROR | WARNING | INFO
    affected_rows: int
    sample_keys: str
    message: str


def validate(cs) -> list:
    f, A = [], cs.activities
    dd = cs.snapshot.data_date
    ids = {a.activity_id for a in A}

    def add(code, sev, rows, msg):
        if rows:
            f.append(Finding(code, sev, len(rows), ",".join(map(str, rows[:5])), msg))

    # --- structural integrity (ERROR: reject) ---------------------------
    dup = [k for k, v in Counter(a.activity_id for a in A).items() if v > 1]
    add("KEY_DUP_ACTIVITY", "ERROR", dup, "duplicate activity_id")

    wbs_ids = {w.wbs_id for w in cs.wbs}
    add("REF_ACTIVITY_WBS", "ERROR",
        [a.activity_id for a in A if a.wbs_id not in wbs_ids],
        "activity references a WBS node not present in this snapshot")

    add("REF_REL_ACTIVITY", "ERROR",
        [f"{r.pred_activity_id}->{r.succ_activity_id}" for r in cs.relationships
         if r.pred_activity_id not in ids or r.succ_activity_id not in ids],
        "relationship references an activity not present in this snapshot")

    res_ids = {r.resource_id for r in cs.resources}
    add("REF_ASSIGN", "ERROR",
        [f"{a.activity_id}/{a.resource_id}" for a in cs.assignments
         if a.activity_id not in ids or a.resource_id not in res_ids],
        "assignment references a missing activity or resource")

    # --- temporal logic (ERROR) -----------------------------------------
    add("DATE_ACTUAL_AFTER_DD", "ERROR",
        [a.activity_id for a in A
         if (a.actual_start and a.actual_start.date() > dd)
         or (a.actual_finish and a.actual_finish.date() > dd)],
        "actual date later than the data date")

    add("DATE_FINISH_BEFORE_START", "ERROR",
        [a.activity_id for a in A
         if a.actual_start and a.actual_finish and a.actual_finish < a.actual_start],
        "actual finish earlier than actual start")

    add("STATUS_COMPLETE_NO_FINISH", "ERROR",
        [a.activity_id for a in A if a.status == "COMPLETE" and not a.actual_finish],
        "status COMPLETE without an actual finish")

    add("STATUS_PROGRESS_NO_START", "ERROR",
        [a.activity_id for a in A if a.status == "IN_PROGRESS" and not a.actual_start],
        "status IN_PROGRESS without an actual start")

    add("STATUS_NOTSTART_HAS_ACTUAL", "ERROR",
        [a.activity_id for a in A
         if a.status == "NOT_STARTED" and (a.actual_start or a.actual_finish)],
        "status NOT_STARTED but carries an actual date")

    # --- measure consistency --------------------------------------------
    add("DUR_COMPLETE_REMAINING", "ERROR",
        [a.activity_id for a in A
         if a.status == "COMPLETE" and (a.remaining_duration_d or 0) > 0.01],
        "COMPLETE activity with remaining duration")

    # tolerance absorbs P6's hour-level rounding on imported durations
    add("DUR_NOTSTART_RD_NE_OD", "WARNING",
        [a.activity_id for a in A
         if a.status == "NOT_STARTED"
         and abs((a.remaining_duration_d or 0) - (a.original_duration_d or 0)) > 0.5],
        "NOT_STARTED activity where remaining duration differs from original by >0.5d")

    add("EV_EXCEEDS_BUDGET", "ERROR",
        [a.activity_id for a in A if a.earned_value > a.budget_value + 0.01],
        "earned value greater than budget")

    add("PCT_OUT_OF_RANGE", "ERROR",
        [a.activity_id for a in A
         if a.pct_complete is not None and not (0.0 <= a.pct_complete <= 1.0)],
        "percent complete outside 0..1")

    # --- schedule quality (WARNING: load, but record) --------------------
    succ = {r.pred_activity_id for r in cs.relationships}
    pred = {r.succ_activity_id for r in cs.relationships}
    open_ends = [a.activity_id for a in A
                 if not a.is_milestone and (a.activity_id not in succ or a.activity_id not in pred)]
    add("QLY_OPEN_ENDS", "WARNING", open_ends, "activity with no predecessor or no successor")

    add("QLY_NEGATIVE_FLOAT", "WARNING",
        [a.activity_id for a in A if (a.total_float_d or 0) < 0],
        "negative total float")

    add("QLY_HIGH_FLOAT", "INFO",
        [a.activity_id for a in A if (a.total_float_d or 0) > 44],
        "total float above the DCMA 44-day threshold")

    add("QLY_HARD_CONSTRAINT", "WARNING",
        [a.activity_id for a in A
         if a.constraint_type in ("CS_MANDSTART", "CS_MANDFIN")],
        "mandatory constraint overrides logic")

    add("QLY_NO_BUDGET", "WARNING",
        [a.activity_id for a in A if a.budget_value <= 0 and not a.is_milestone],
        "activity carries no budget value, so contributes nothing to earned value")

    add("QLY_NO_DRIVING_PATH", "WARNING",
        ["snapshot"] if not any(a.is_driving_path for a in A) else [],
        "no activity flagged on the driving path")

    return f


def has_errors(findings):
    return any(x.severity == "ERROR" for x in findings)
