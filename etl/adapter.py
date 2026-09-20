"""Source adapters. XER is implemented; REST and PMDB are declared so the
boundary is real rather than aspirational.

P6 Professional (standalone) has no API — the database is a local file with
no service layer. Even where P6 EPPM is deployed, schedules move between
organisations as XER files, because the schedule owner is usually a PMC or
subcontractor whose P6 instance you will never be given credentials to.
File-drop ingestion is the normal case, not the fallback.
"""
from __future__ import annotations
import hashlib, os, re, datetime as dt
from abc import ABC, abstractmethod
from typing import Optional

from canonical import (CanonicalSchedule, Project, SnapshotMeta, WbsNode,
                       Activity, Relationship, Resource, Assignment)
from p6cal import Calendar


class SourceAdapter(ABC):
    @abstractmethod
    def fetch(self) -> CanonicalSchedule: ...


STATUS = {"TK_NotStart": "NOT_STARTED", "TK_Active": "IN_PROGRESS", "TK_Complete": "COMPLETE"}
RELTYPE = {"PR_FS": "FS", "PR_SS": "SS", "PR_FF": "FF", "PR_SF": "SF"}
MILESTONE_TYPES = {"TT_Mile", "TT_FinMile"}
LOE_TYPES = {"TT_LOE", "TT_WBS"}


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _ts(s):
    if not s:
        return None
    try:
        return dt.datetime.strptime(s[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        return None


class XerFileAdapter(SourceAdapter):
    def __init__(self, path: str, project_id: Optional[str] = None,
                 snapshot_type: Optional[str] = None, config: Optional[dict] = None):
        self.path = path
        self.want_project = project_id
        self.forced_type = snapshot_type
        self.cfg = config or {}

    @staticmethod
    def _parse(path, encoding=None):
        """XER is tab-delimited with line-type markers:
             %T <table>   start a table
             %F <cols>    its column names
             %R <values>  one row
        P6 writes cp1252; some third-party tools write UTF-8. Try both rather
        than failing on one non-ASCII character in an activity name.
        """
        encodings = [encoding] if encoding else ["cp1252", "utf-8-sig", "latin-1"]
        last = None
        for enc in encodings:
            try:
                tables, cur = {}, None
                with open(path, "r", encoding=enc, newline="") as fh:
                    for line in fh:
                        parts = line.rstrip("\r\n").split("\t")
                        tag = parts[0]
                        if tag == "%T":
                            cur = parts[1]; tables[cur] = {"fields": [], "rows": []}
                        elif tag == "%F":
                            tables[cur]["fields"] = parts[1:]
                        elif tag == "%R":
                            fl = tables[cur]["fields"]
                            vals = parts[1:]
                            if len(vals) < len(fl):
                                vals += [""] * (len(fl) - len(vals))
                            tables[cur]["rows"].append(dict(zip(fl, vals[:len(fl)])))
                return tables
            except UnicodeDecodeError as exc:
                last = exc
        raise last

    @staticmethod
    def _sha256(path):
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    def fetch(self) -> CanonicalSchedule:
        t = self._parse(self.path)

        # FIX 1: an XER may hold several projects (baselines export alongside).
        projects = t["PROJECT"]["rows"]
        if self.want_project:
            projects = [r for r in projects if r["proj_short_name"] == self.want_project]
            if not projects:
                raise ValueError(f"project {self.want_project} not in {self.path}")
        if len(projects) > 1:
            raise ValueError(f"{self.path} holds {len(projects)} projects; pass project_id")
        proj = projects[0]
        proj_id = proj["proj_id"]

        so = (t.get("SCHEDOPTIONS", {"rows": []})["rows"] or [{}])[0]
        dd_ts = _ts(proj.get("last_recalc_date"))
        if dd_ts is None:
            raise ValueError(f"{self.path}: no data date (PROJECT.last_recalc_date)")
        data_date = dd_ts.date()

        def rows(table):
            rs = t.get(table, {"rows": []})["rows"]
            return [r for r in rs if r.get("proj_id", proj_id) == proj_id]

        project = Project(
            project_id=proj["proj_short_name"],
            project_name=self.cfg.get("project_name", proj["proj_short_name"]),
            currency_code=self.cfg.get("currency_code", "INR"),
            value_basis=self.cfg.get("value_basis", "currency"),
            # FIX 2: a missing date must be None, not year 1 via datetime.min
            planned_start=(_ts(proj.get("plan_start_date")) or dd_ts).date(),
            planned_finish=(_ts(proj.get("scd_end_date")).date()
                            if _ts(proj.get("scd_end_date")) else None),
        )
        snap = SnapshotMeta(
            data_date=data_date,
            snapshot_type=self.forced_type or "UPDATE",
            # FIX 3: os.path.basename handles Windows backslashes
            source_file=os.path.basename(self.path),
            file_sha256=self._sha256(self.path),
            retained_logic=(so.get("sched_retained_logic") == "Y") if so else None,
            # FIX 4: critical_path_type is in PROJECT, not SCHEDOPTIONS. Reading
            # the wrong table with .get() silently wrote NULL instead of failing.
            critical_path_type=proj.get("critical_path_type") or None,
            default_pct_type=proj.get("def_complete_pct_type"),
            project_finish=_ts(proj.get("scd_end_date")),
            must_finish_by=_ts(proj.get("plan_end_date")),
        )

        # calendars ------------------------------------------------------
        cal_rows = {c["clndr_id"]: c for c in t.get("CALENDAR", {"rows": []})["rows"]}
        hpd = {cid: (_f(c.get("day_hr_cnt"), 8.0) or 8.0) for cid, c in cal_rows.items()}
        cals = {cid: Calendar(cid, c["clndr_name"], c.get("clndr_data", ""), hpd[cid])
                for cid, c in cal_rows.items()}
        default_cal = proj.get("clndr_id") or (next(iter(hpd)) if hpd else None)

        def hours_per_day(cid):
            return hpd.get(cid, hpd.get(default_cal, 8.0))

        # WBS ------------------------------------------------------------
        wrows = {r["wbs_id"]: r for r in rows("PROJWBS")}

        def chain(wid):
            out, seen = [], set()
            while wid in wrows and wid not in seen:
                seen.add(wid); out.append(wrows[wid]); wid = wrows[wid]["parent_wbs_id"]
            return list(reversed(out))

        area_pat = re.compile(self.cfg.get(
            "area_pattern", r"^(Tower \d+|Central Club House|External Development Works)"))
        phase_map = self.cfg.get("phase_map", {
            "Structure works": "Structure", "Finishing": "Finishes",
            "Testing And Commissioning": "T&C", "Foundation": "Foundation",
            "Procurement": "Procurement",
        })

        wbs = []
        for wid, r in wrows.items():
            ch = chain(wid)
            names = [c["wbs_name"] for c in ch]
            area = next((area_pat.match(n).group(1) for n in names if area_pat.match(n)), None)
            phase = next((v for n in names for k, v in phase_map.items() if k in n), None)
            wbs.append(WbsNode(
                wbs_id=wid,
                wbs_code=".".join(c["wbs_short_name"] for c in ch),
                wbs_name=r["wbs_name"],
                parent_wbs_id=r["parent_wbs_id"] if r["parent_wbs_id"] in wrows else None,
                wbs_level=len(ch),
                wbs_path=" > ".join(names),
                area=area, phase=phase,
            ))

        # resources ------------------------------------------------------
        rrows = {r["rsrc_id"]: r for r in t.get("RSRC", {"rows": []})["rows"]}
        rates = {}
        for r in t.get("RSRCRATE", {"rows": []})["rows"]:
            rates.setdefault(r["rsrc_id"], _f(r.get("cost_per_qty")))
        resources = [Resource(
            resource_id=r["rsrc_short_name"],
            resource_name=r["rsrc_name"],
            resource_type=(r.get("rsrc_type") or "").replace("RT_", "") or None,
            unit_of_measure=r.get("unit_id") or None,
            price_per_unit=rates.get(rid),          # FIX 5: was never populated
            contractor_name=r["rsrc_name"],
        ) for rid, r in rrows.items()]

        # assignments -----------------------------------------------------
        task_rows = rows("TASK")
        idmap = {r["task_id"]: r["task_code"] for r in task_rows}
        calmap = {r["task_code"]: r["clndr_id"] for r in task_rows}
        assignments, budget = [], {}
        for x in rows("TASKRSRC"):
            aid = idmap.get(x["task_id"])
            if aid is None or x["rsrc_id"] not in rrows:
                continue
            b = _f(x.get("target_qty"), 0.0) or 0.0
            assignments.append(Assignment(
                activity_id=aid,
                resource_id=rrows[x["rsrc_id"]]["rsrc_short_name"],
                budgeted_units=b,
                actual_units=(_f(x.get("act_reg_qty"), 0.0) or 0.0) + (_f(x.get("act_ot_qty"), 0.0) or 0.0),
                remaining_units=_f(x.get("remain_qty"), 0.0) or 0.0,
                budgeted_cost=_f(x.get("target_cost"), 0.0) or 0.0,
                actual_cost=(_f(x.get("act_reg_cost"), 0.0) or 0.0) + (_f(x.get("act_ot_cost"), 0.0) or 0.0),
                remaining_cost=_f(x.get("remain_cost"), 0.0) or 0.0,
            ))
            budget[aid] = budget.get(aid, 0.0) + b

        # activities -------------------------------------------------------
        activities = []
        for r in task_rows:
            cid = r["clndr_id"]
            h = hours_per_day(cid)
            od = (_f(r.get("target_drtn_hr_cnt"), 0.0) or 0.0) / h
            rd = (_f(r.get("remain_drtn_hr_cnt"), 0.0) or 0.0) / h
            status = STATUS.get(r["status_code"], "NOT_STARTED")
            a_start, a_finish = _ts(r.get("act_start_date")), _ts(r.get("act_end_date"))

            # FIX 6: TASK has no actual-duration column (act_work_qty is labour
            # units, not time). Derive it as working time from actual start to
            # actual finish, or to the data date if still running. Without it
            # at_completion_duration and every duration variance stayed NULL.
            actual_d = None
            if a_start:
                end = a_finish or dd_ts
                cal = cals.get(cid)
                if cal and end >= a_start:
                    actual_d = round(cal.diff_hours(a_start, end) / h, 3)

            if status == "COMPLETE":
                pct = 1.0
            elif status == "NOT_STARTED" or od <= 0:
                pct = 0.0
            else:
                # floored at zero: an overrunning activity can carry RD > OD,
                # which P6 clamps rather than reporting negative progress
                pct = max(0.0, min(1.0, (od - rd) / od))
            bud = budget.get(r["task_code"], 0.0)
            tf_raw, ff_raw = r.get("total_float_hr_cnt"), r.get("free_float_hr_cnt")

            a = Activity(
                activity_id=r["task_code"],
                activity_name=r["task_name"],
                activity_type=r["task_type"],
                wbs_id=r["wbs_id"],
                calendar_id=cid,
                duration_type=r.get("duration_type"),
                pct_complete_type=r.get("complete_pct_type"),
                is_milestone=r["task_type"] in MILESTONE_TYPES,
                status=status,
                original_duration_d=round(od, 3),
                remaining_duration_d=round(rd, 3),
                actual_duration_d=actual_d,
                early_start=_ts(r.get("early_start_date")),
                early_finish=_ts(r.get("early_end_date")),
                late_start=_ts(r.get("late_start_date")),
                late_finish=_ts(r.get("late_end_date")),
                actual_start=a_start,
                actual_finish=a_finish,
                planned_start=_ts(r.get("target_start_date")),
                planned_finish=_ts(r.get("target_end_date")),
                # a completed activity carries no float; '' must stay None or
                # every finished activity looks critical
                total_float_d=round((_f(tf_raw, 0.0) or 0.0) / h, 3) if tf_raw not in (None, "") else None,
                free_float_d=round((_f(ff_raw, 0.0) or 0.0) / h, 3) if ff_raw not in (None, "") else None,
                # FIX 7: was False for a completed activity; should be None
                is_critical=((_f(tf_raw, 1.0) or 1.0) <= 0) if tf_raw not in (None, "") else None,
                is_driving_path=(r.get("driving_path_flag") == "Y"),
                constraint_type=r.get("cstr_type") or None,
                constraint_date=_ts(r.get("cstr_date")),
                pct_complete=round(pct, 4),
                budget_value=bud,
                earned_value=round(bud * pct, 2),
            )
            a.is_loe = r["task_type"] in LOE_TYPES     # FIX 8: never set before
            activities.append(a)

        # relationships -----------------------------------------------------
        # FIX 9: lag was divided by the FIRST activity's hours-per-day for every
        # link. SCHEDOPTIONS.sched_calendar_on_relationship_lag says which
        # calendar governs; P6 defaults to the predecessor's.
        lag_basis = so.get("sched_calendar_on_relationship_lag", "rcal_Predecessor")
        rels, seen = [], set()
        for r in rows("TASKPRED"):
            p, s = idmap.get(r["pred_task_id"]), idmap.get(r["task_id"])
            if p is None or s is None:
                continue
            rtype = RELTYPE.get(r["pred_type"], "FS")
            key = (p, s, rtype)
            if key in seen:            # P6 allows one link per pair+type
                continue
            seen.add(key)
            basis = p if lag_basis == "rcal_Predecessor" else s
            h = hours_per_day(calmap.get(basis, default_cal))
            rels.append(Relationship(
                pred_activity_id=p, succ_activity_id=s,
                relationship_type=rtype,
                lag_d=round((_f(r.get("lag_hr_cnt"), 0.0) or 0.0) / h, 3),
            ))

        return CanonicalSchedule(
            project=project, snapshot=snap, wbs=wbs, activities=activities,
            relationships=rels, resources=resources, assignments=assignments,
            calendar_days=[],          # filled by the calendar builder
        )


class P6EppmRestAdapter(SourceAdapter):
    """Declared, not implemented. Requires a P6 EPPM deployment."""
    def __init__(self, base_url: str, api_key: str, project_id: str):
        self.base_url, self.api_key, self.project_id = base_url, api_key, project_id

    def fetch(self) -> CanonicalSchedule:
        raise NotImplementedError("P6 EPPM REST adapter not implemented")


class PmdbSqlAdapter(SourceAdapter):
    """Declared, not implemented. Direct read of a P6 EPPM PMDB schema."""
    def __init__(self, dsn: str, project_id: str):
        self.dsn, self.project_id = dsn, project_id

    def fetch(self) -> CanonicalSchedule:
        raise NotImplementedError("PMDB adapter not implemented")
