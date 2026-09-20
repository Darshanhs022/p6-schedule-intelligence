#!/usr/bin/env python3
"""DAG-style orchestration over the ingestion pipeline.

Tasks are explicit and independently re-runnable. The shape maps directly
onto Airflow/Prefect if you later want scheduling, retries and alerting:
each `run_*` becomes a task and TASK_GRAPH becomes the dependency graph.
Run it by hand today, schedule it tomorrow, without rewriting the logic.

  discover -> parse -> build_calendar -> validate -> load -> audit
"""
from __future__ import annotations
import argparse, glob, os, sys, datetime as dt
import yaml

from adapter import XerFileAdapter
from calendar_builder import build_calendar_days
from validate import validate, has_errors
from loader import WarehouseLoader

TASK_GRAPH = {
    "discover":       [],
    "parse":          ["discover"],
    "build_calendar": ["parse"],
    "validate":       ["parse", "build_calendar"],
    "load":           ["validate"],
    "audit":          ["load"],
}

ADAPTERS = {"xer": XerFileAdapter}


def log(task, msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {task:<15} {msg}", flush=True)


def run_discover(cfg, source_dir):
    pattern = os.path.join(source_dir, cfg["source"]["file_pattern"])
    files = sorted(glob.glob(pattern))
    if not files:
        raise SystemExit(f"no files matched {pattern}")
    baselines = set(cfg["source"].get("baseline_files") or [])
    ordered = []
    for f in files:
        is_bl = os.path.basename(f) in baselines
        ordered.append((f, "BASELINE" if is_bl else "UPDATE"))
    # baseline first, then updates by data date (filename carries it)
    ordered.sort(key=lambda x: (x[1] != "BASELINE", x[0]))
    log("discover", f"{len(ordered)} file(s); {sum(1 for _,t in ordered if t=='BASELINE')} baseline")
    return ordered


def run_parse(cfg, path, snap_type):
    adapter_name = cfg["source"]["adapter"]
    if adapter_name not in ADAPTERS:
        raise SystemExit(f"adapter '{adapter_name}' not implemented")
    acfg = dict(cfg["project"])
    acfg.update(cfg.get("dimensions", {}))
    cs = ADAPTERS[adapter_name](path, snapshot_type=snap_type, config=acfg).fetch()
    log("parse", f"{os.path.basename(path)} dd={cs.snapshot.data_date} {cs.summary()}")
    return cs


def run_build_calendar(cfg, cs, path):
    c = cfg["calendar"]
    tables = XerFileAdapter._parse(path)
    cs.calendar_days = build_calendar_days(tables, c["start"], c["end"])
    log("build_calendar", f"{len(cs.calendar_days)} calendar-day rows")
    return cs


def run_validate(cfg, cs):
    f = validate(cs)
    errs = [x for x in f if x.severity == "ERROR"]
    warns = [x for x in f if x.severity == "WARNING"]
    log("validate", f"{len(errs)} error(s), {len(warns)} warning(s)")
    for x in errs:
        log("validate", f"   ERROR {x.rule_code} rows={x.affected_rows} {x.message}")
    return f


def run_load(cfg, cs):
    dsn = {k: v for k, v in cfg["database"].items() if k in
           ("host", "port", "dbname", "user", "password")}
    if "PGPASSWORD" in os.environ:
        dsn["password"] = os.environ["PGPASSWORD"]
    res = WarehouseLoader(dsn, cfg).load(cs)
    log("load", f"{res['status']} snapshot_key={res.get('snapshot_key')}")
    return res


def main():
    ap = argparse.ArgumentParser(description="P6 schedule warehouse pipeline")
    ap.add_argument("--config", required=True)
    ap.add_argument("--source", required=True, help="directory containing XER files")
    ap.add_argument("--dry-run", action="store_true", help="parse and validate only")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.config))
    files = run_discover(cfg, args.source)

    ok = skipped = rejected = 0
    for path, snap_type in files:
        print("-" * 78)
        cs = run_parse(cfg, path, snap_type)
        cs = run_build_calendar(cfg, cs, path)
        findings = run_validate(cfg, cs)

        if has_errors(findings) and cfg["validation"]["reject_on_error"]:
            log("load", "SKIPPED — validation errors, file rejected")
            rejected += 1
            if args.dry_run:
                continue
        if args.dry_run:
            continue

        res = run_load(cfg, cs)
        if res["status"] == "LOADED":
            ok += 1
        elif res["status"] == "SKIPPED_DUPLICATE":
            skipped += 1
        else:
            rejected += 1

    print("=" * 78)
    log("audit", f"loaded={ok} skipped={skipped} rejected={rejected}")
    return 1 if rejected else 0


if __name__ == "__main__":
    sys.exit(main())
