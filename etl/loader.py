"""Warehouse loader.

Properties that matter:
  * IDEMPOTENT   - a file is identified by SHA-256; re-running skips it.
  * APPEND-ONLY  - snapshots are immutable once written. History cannot be
                   rewritten, which is the whole point of a snapshot model.
  * ALL-OR-NOTHING - one transaction per file. A validation ERROR or any
                   database failure rolls back completely; there is never a
                   partially loaded snapshot.
  * AUDITED      - every attempt writes a row, including rejections.
"""
from __future__ import annotations
import datetime as dt
import psycopg2
import psycopg2.extras as pgx

from validate import validate, has_errors


class WarehouseLoader:
    def __init__(self, dsn: dict, cfg: dict):
        self.dsn, self.cfg = dsn, cfg

    # -- helpers ---------------------------------------------------------
    def _conn(self):
        return psycopg2.connect(**self.dsn)

    @staticmethod
    def _one(cur, sql, params=()):
        cur.execute(sql, params)
        r = cur.fetchone()
        return r[0] if r else None

    # -- dimension upserts ------------------------------------------------
    def _project_key(self, cur, p):
        k = self._one(cur, "SELECT project_key FROM core.dim_project WHERE project_id=%s",
                      (p.project_id,))
        if k:
            return k
        return self._one(cur, """
            INSERT INTO core.dim_project
                (project_id, project_name, client_name, currency_code, value_basis,
                 planned_start, planned_finish)
            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING project_key""",
            (p.project_id, p.project_name, self.cfg["project"].get("client_name"),
             p.currency_code, p.value_basis, p.planned_start, p.planned_finish))

    def _baseline_key(self, cur, project_key):
        b = self.cfg["baseline"]
        k = self._one(cur, "SELECT baseline_key FROM core.dim_baseline WHERE project_key=%s AND baseline_code=%s",
                      (project_key, b["code"]))
        if k:
            return k
        return self._one(cur, """
            INSERT INTO core.dim_baseline
                (project_key, baseline_code, baseline_type, effective_from, is_original)
            VALUES (%s,%s,%s,%s,%s) RETURNING baseline_key""",
            (project_key, b["code"], b.get("type"), b["effective_from"],
             bool(b.get("is_original"))))

    def _calendar_days(self, cur, project_key, days):
        """Load per CALENDAR, not per project.

        The previous version skipped the whole build once any calendar day
        existed for the project. A calendar added later — or a revised holiday
        list — would then never load, and every variance joining to a missing
        cal_date would return NULL silently rather than failing.
        """
        if not days:
            return
        cur.execute("SELECT DISTINCT calendar_key FROM core.dim_calendar_day WHERE project_key=%s",
                    (project_key,))
        have = {r[0] for r in cur.fetchall()}
        days = [c for c in days if int(c.calendar_id) not in have]
        if not days:
            return
        rows, idx = [], {}
        for c in sorted(days, key=lambda x: (x.calendar_id, x.cal_date)):
            n = idx.get(c.calendar_id, 0) + (1 if c.is_working_day else 0)
            idx[c.calendar_id] = n
            rows.append((int(c.calendar_id), project_key, c.calendar_name, c.cal_date,
                         c.working_hours, c.is_working_day, c.is_exception, n))
        pgx.execute_values(cur, """
            INSERT INTO core.dim_calendar_day
              (calendar_key, project_key, calendar_name, cal_date, working_hours,
               is_working_day, is_exception, workday_index)
            VALUES %s ON CONFLICT DO NOTHING""", rows, page_size=5000)

    def _wbs_keys(self, cur, project_key, wbs, data_date):
        """SCD2: close a row and open a new one when attributes change."""
        cur.execute("""SELECT wbs_id, wbs_key, wbs_code, wbs_name, parent_wbs_id,
                              wbs_level, wbs_path, area, phase
                       FROM core.dim_wbs WHERE project_key=%s AND is_current""",
                    (project_key,))
        cur_rows = {r[0]: r for r in cur.fetchall()}
        keys, ins, close = {}, [], []
        for w in wbs:
            e = cur_rows.get(w.wbs_id)
            same = e and (e[2], e[3], e[4], e[5], e[6], e[7], e[8]) == (
                w.wbs_code, w.wbs_name, w.parent_wbs_id, w.wbs_level, w.wbs_path, w.area, w.phase)
            if same:
                keys[w.wbs_id] = e[1]
            else:
                if e:
                    close.append(e[1])
                ins.append((project_key, w.wbs_id, w.wbs_code, w.wbs_name, w.parent_wbs_id,
                            w.wbs_level, w.wbs_path, w.area, w.phase, data_date))
        if close:
            cur.execute("UPDATE core.dim_wbs SET is_current=FALSE, valid_to=%s WHERE wbs_key = ANY(%s)",
                        (data_date, close))
        if ins:
            out = pgx.execute_values(cur, """
                INSERT INTO core.dim_wbs
                  (project_key, wbs_id, wbs_code, wbs_name, parent_wbs_id,
                   wbs_level, wbs_path, area, phase, valid_from)
                VALUES %s RETURNING wbs_key, wbs_id""", ins, fetch=True)
            for k, wid in out:
                keys[wid] = k
        return keys

    def _activity_keys(self, cur, project_key, acts, data_date):
        cur.execute("""SELECT activity_id, activity_key, activity_name, activity_type,
                              duration_type, pct_complete_type, calendar_key, wbs_id,
                              is_milestone, is_loe
                       FROM core.dim_activity WHERE project_key=%s AND is_current""",
                    (project_key,))
        cur_rows = {r[0]: r for r in cur.fetchall()}
        keys, ins, close = {}, [], []
        for a in acts:
            e = cur_rows.get(a.activity_id)
            cal = int(a.calendar_id) if a.calendar_id else None
            same = e and (e[2], e[3], e[4], e[5], e[6], e[7], e[8], e[9]) == (
                a.activity_name, a.activity_type, a.duration_type,
                a.pct_complete_type, cal, a.wbs_id, a.is_milestone, a.is_loe)
            if same:
                keys[a.activity_id] = e[1]
            else:
                if e:
                    close.append(e[1])
                ins.append((project_key, a.activity_id, a.activity_name, a.activity_type,
                            a.duration_type, a.pct_complete_type, cal, a.wbs_id,
                            a.is_milestone, a.is_loe, data_date))   # FIX: was hardcoded False
        if close:
            cur.execute("UPDATE core.dim_activity SET is_current=FALSE, valid_to=%s WHERE activity_key = ANY(%s)",
                        (data_date, close))
        if ins:
            out = pgx.execute_values(cur, """
                INSERT INTO core.dim_activity
                  (project_key, activity_id, activity_name, activity_type, duration_type,
                   pct_complete_type, calendar_key, wbs_id, is_milestone, is_loe, valid_from)
                VALUES %s RETURNING activity_key, activity_id""", ins, fetch=True)
            for k, aid in out:
                keys[aid] = k
        return keys

    def _resource_keys(self, cur, project_key, resources):
        keys = {}
        for r in resources:
            k = self._one(cur, "SELECT resource_key FROM core.dim_resource WHERE project_key=%s AND resource_id=%s",
                          (project_key, r.resource_id))
            if not k:
                k = self._one(cur, """
                    INSERT INTO core.dim_resource
                      (project_key, resource_id, resource_name, resource_type,
                       unit_of_measure, price_per_unit, contractor_name, trade)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING resource_key""",
                    (project_key, r.resource_id, r.resource_name, r.resource_type,
                     r.unit_of_measure, r.price_per_unit, r.contractor_name, r.trade))
            keys[r.resource_id] = k
        return keys

    # -- main entry point --------------------------------------------------
    def load(self, cs) -> dict:
        started = dt.datetime.now()
        findings = validate(cs)
        errors = has_errors(findings)

        conn = self._conn()
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                pk = self._project_key(cur, cs.project)

                dup = self._one(cur, "SELECT snapshot_key FROM core.dim_snapshot WHERE project_key=%s AND file_sha256=%s",
                                (pk, cs.snapshot.file_sha256))
                if dup:
                    self._audit(cur, pk, dup, cs, "SKIPPED_DUPLICATE", findings, started,
                                "identical file already loaded")
                    conn.commit()
                    return {"status": "SKIPPED_DUPLICATE", "snapshot_key": dup}

                if errors:
                    self._audit(cur, pk, None, cs, "REJECTED", findings, started,
                                "; ".join(f"{f.rule_code}({f.affected_rows})"
                                          for f in findings if f.severity == "ERROR"))
                    conn.commit()                      # keep the audit row
                    return {"status": "REJECTED",
                            "errors": [f.rule_code for f in findings if f.severity == "ERROR"]}

                bk = self._baseline_key(cur, pk)
                self._calendar_days(cur, pk, cs.calendar_days)

                seq = (self._one(cur, "SELECT COALESCE(MAX(snapshot_seq),0)+1 FROM core.dim_snapshot WHERE project_key=%s",
                                 (pk,)) or 1)
                s = cs.snapshot
                sk = self._one(cur, """
                    INSERT INTO core.dim_snapshot
                      (project_key, baseline_key, data_date, snapshot_type, snapshot_seq,
                       source_system, source_format, source_file, file_sha256,
                       retained_logic, critical_path_type, default_pct_type,
                       project_finish, must_finish_by)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING snapshot_key""",
                    (pk, bk, s.data_date, s.snapshot_type, seq, s.source_system, s.source_format,
                     s.source_file, s.file_sha256, s.retained_logic, s.critical_path_type,
                     s.default_pct_type, s.project_finish, s.must_finish_by))

                wkeys = self._wbs_keys(cur, pk, cs.wbs, s.data_date)
                akeys = self._activity_keys(cur, pk, cs.activities, s.data_date)
                rkeys = self._resource_keys(cur, pk, cs.resources)

                pgx.execute_values(cur, """
                    INSERT INTO core.fact_activity_snapshot
                      (snapshot_key, activity_key, wbs_key, project_key, data_date, status,
                       original_duration_d, remaining_duration_d, actual_duration_d,
                       at_completion_dur_d, early_start, early_finish, late_start, late_finish,
                       actual_start, actual_finish, planned_start, planned_finish,
                       total_float_d, free_float_d, is_critical, is_driving_path,
                       constraint_type, constraint_date, pct_complete, budget_value, earned_value)
                    VALUES %s""",
                    [(sk, akeys[a.activity_id], wkeys[a.wbs_id], pk, s.data_date, a.status,
                      a.original_duration_d, a.remaining_duration_d, a.actual_duration_d,
                      a.at_completion_duration_d, a.early_start, a.early_finish,
                      a.late_start, a.late_finish, a.actual_start, a.actual_finish,
                      a.planned_start, a.planned_finish, a.total_float_d, a.free_float_d,
                      a.is_critical, a.is_driving_path, a.constraint_type, a.constraint_date,
                      a.pct_complete, a.budget_value, a.earned_value)
                     for a in cs.activities], page_size=2000)

                seen = set()
                relrows = []
                for r in cs.relationships:
                    key = (r.pred_activity_id, r.succ_activity_id, r.relationship_type)
                    if key in seen:
                        continue                     # P6 permits only one link per pair+type
                    seen.add(key)
                    relrows.append((sk, akeys[r.pred_activity_id], akeys[r.succ_activity_id],
                                    pk, s.data_date, r.relationship_type, r.lag_d, r.is_driving))
                pgx.execute_values(cur, """
                    INSERT INTO core.fact_relationship_snapshot
                      (snapshot_key, pred_activity_key, succ_activity_key, project_key,
                       data_date, relationship_type, lag_d, is_driving)
                    VALUES %s""", relrows, page_size=2000)

                pgx.execute_values(cur, """
                    INSERT INTO core.fact_assignment_snapshot
                      (snapshot_key, activity_key, resource_key, project_key, data_date,
                       budgeted_units, actual_units, remaining_units,
                       budgeted_cost, actual_cost, remaining_cost)
                    VALUES %s ON CONFLICT DO NOTHING""",
                    [(sk, akeys[a.activity_id], rkeys[a.resource_id], pk, s.data_date,
                      a.budgeted_units, a.actual_units, a.remaining_units,
                      a.budgeted_cost, a.actual_cost, a.remaining_cost)
                     for a in cs.assignments if a.activity_id in akeys], page_size=2000)

                self._audit(cur, pk, sk, cs, "LOADED", findings, started, None)
            conn.commit()
            return {"status": "LOADED", "snapshot_key": sk, "findings": findings}
        except Exception as exc:
            conn.rollback()
            with self._conn() as c2, c2.cursor() as cur2:
                self._audit(cur2, None, None, cs, "FAILED", findings, started, str(exc)[:500])
                c2.commit()
            raise
        finally:
            conn.close()

    def _audit(self, cur, pk, sk, cs, status, findings, started, reason):
        s = cs.snapshot
        lk = self._one(cur, """
            INSERT INTO core.fact_load_audit
              (project_key, snapshot_key, source_file, file_sha256, data_date, status,
               rows_activities, rows_relationships, rows_assignments,
               checks_passed, checks_failed, reject_reason, started_at, finished_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING load_key""",
            (pk, sk, s.source_file, s.file_sha256, s.data_date, status,
             len(cs.activities), len(cs.relationships), len(cs.assignments),
             sum(1 for f in findings if f.severity != "ERROR"),
             sum(1 for f in findings if f.severity == "ERROR"),
             reason, started, dt.datetime.now()))
        if findings:
            pgx.execute_values(cur, """
                INSERT INTO core.fact_validation_result
                  (load_key, rule_code, severity, affected_rows, sample_keys, message)
                VALUES %s""",
                [(lk, f.rule_code, f.severity, f.affected_rows, f.sample_keys, f.message)
                 for f in findings])
