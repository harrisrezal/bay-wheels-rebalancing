#!/usr/bin/env python3
"""Rebuild the analysis in dependency order, and refuse to lie about freshness.

This exists because of a bug that recurred four times: a downstream table quietly
kept serving old numbers while newer results were being described. The worst case
went unnoticed for days — station_deltas was still built from a 2-day grid while
grid_status covered 7, so the van recalibration, the overnight finding and the demand
estimate were all computed on 2 days and reported as a week.

The order below is the actual dependency graph. It only lived in my head before, which
is precisely why it kept being violated.

  sync            Supabase -> local DuckDB
  grid            5-minute observation grid, forward-filled
  deltas          per-interval flow, initial crude van labels
  calibrate_vans  demand-aware van thresholds (iterates with demand)
  demand          censoring-corrected demand baseline
  outages         discrete demand-qualified failures
  forecast        starvation prediction, horizon sweep        (read-only)
  dispatch        rebalancing policy backtest                 (read-only)
  export          static JSON for the web app

--check verifies freshness without rebuilding, so it is safe to run before quoting a
number. Exit code 2 means something downstream is older than its input.
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "analysis.duckdb"
PY_BIN = ROOT / ".venv" / "bin" / "python"

# (step, script, table it produces, time column) — None means no table to timestamp
STEPS = [
    ("sync",           "analysis/sync_from_postgres.py", "station_status",  "observed_at"),
    ("grid",           "analysis/build_grid.py",         "grid_status",     "bucket"),
    ("deltas",         "analysis/build_deltas.py",       "station_deltas",  "bucket"),
    ("calibrate_vans", "analysis/calibrate_vans.py",     "station_deltas",  "bucket"),
    ("demand",         "analysis/build_demand.py",       "demand_baseline", None),
    ("outages",        "analysis/build_outages.py",      "outage_events",   "ended_at"),
    ("forecast",       "analysis/build_forecast.py",     None,              None),
    ("dispatch",       "analysis/build_dispatch.py",     None,              None),
    ("export",         "analysis/export_web.py",         None,              None),
]


def latest(con, table, col):
    if not table or not col:
        return None
    try:
        return con.execute(f"SELECT max({col}) FROM {table}").fetchone()[0]
    except Exception:
        return None


# A downstream table is legitimately a little behind its input: the grid only covers
# complete buckets, so it trails station_status by one bucket plus poll lag. Flagging
# that as stale would make the check cry wolf, and a check people learn to ignore is
# worse than no check. Days behind is the real failure; minutes is normal.
TOLERANCE_MIN = 90


def check():
    """Report freshness. Non-zero exit if a table lags its input beyond tolerance."""
    con = duckdb.connect(str(DB), read_only=True)
    stale = []
    seen = set()
    print(f"  {'table':<18} {'latest data':<18} {'lag':>9}  state")
    prev_name = prev_ts = None
    for name, _, table, col in STEPS:
        if not table or not col or table in seen:
            continue
        seen.add(table)
        ts = latest(con, table, col)
        if ts is None:
            print(f"  {table:<18} {'(missing)':<18} {'':>9}  MISSING")
            stale.append(name)
            prev_name, prev_ts = name, None
            continue
        lag_min = (prev_ts - ts).total_seconds() / 60 if prev_ts else 0
        flag = "ok"
        if lag_min > TOLERANCE_MIN:
            flag = f"STALE — {lag_min/60:.1f}h behind {prev_name}"
            stale.append(name)
        lag_s = f"{lag_min:>6.0f}m" if prev_ts else "     —"
        print(f"  {table:<18} {ts:%Y-%m-%d %H:%M}   {lag_s}  {flag}")
        prev_name, prev_ts = name, ts
    # Internal consistency is not enough: every table can agree with every other and
    # all of them be days behind Supabase, which is exactly the state this check was
    # written after finding.
    try:
        sys.path.insert(0, str(ROOT))
        from lib import store_pg
        pg = store_pg.connect()
        src = pg.execute("SELECT max(observed_at) FROM station_status").fetchone()[0]
        pg.close()
        local = latest(con, "station_status", "observed_at")
        if src and local:
            behind = (src - local).total_seconds() / 3600
            print(f"\n  Supabase has data to {src.astimezone():%Y-%m-%d %H:%M} "
                  f"— local is {behind:.1f}h behind")
            if behind > TOLERANCE_MIN / 60:
                stale.append("sync")
    except Exception as e:
        print(f"\n  (could not reach Supabase to compare: {type(e).__name__})")

    con.close()
    if stale:
        print(f"\n  {len(stale)} table(s) stale: {', '.join(stale)}")
        print("  Numbers read from these do not describe the data you think they do.")
        return 2
    print("\n  all tables consistent")
    return 0


def run(only=None):
    failed = []
    for name, script, _, _ in STEPS:
        if only and name not in only:
            continue
        print(f"\n{'='*62}\n  {name}\n{'='*62}")
        t0 = time.time()
        r = subprocess.run([str(PY_BIN), script], cwd=ROOT)
        dt = time.time() - t0
        if r.returncode != 0:
            print(f"\n  !! {name} failed (exit {r.returncode}) after {dt:.0f}s")
            print("  Stopping: later steps would build on a broken table.")
            failed.append(name)
            break
        print(f"  -- {name} done in {dt:.0f}s")
    if failed:
        return 1
    print(f"\n{'='*62}\n  freshness check\n{'='*62}")
    return check()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="verify freshness, rebuild nothing")
    ap.add_argument("--only", nargs="*", help="run only these steps")
    a = ap.parse_args()
    if a.check:
        return check()
    return run(a.only)


if __name__ == "__main__":
    sys.exit(main())
