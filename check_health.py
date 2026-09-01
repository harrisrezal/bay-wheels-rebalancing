#!/usr/bin/env python3
"""Collection health audit — the gate on PRD Phase 0.

Phase 1 cannot start until this reports 7 consecutive days at <2% missing polls. Run it
daily; if the gap rate drifts above the gate, move the poller to an always-on host before
burning more calendar time.

Usage:  python3 check_health.py [--days N]
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("BW_DB", ROOT / "data" / "baywheels.db"))
POLL_INTERVAL_S = 300
GAP_GATE_PCT = 2.0
GATE_DAYS = 7


def fmt_dur(seconds):
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    parts = [f"{d}d" if d else "", f"{h}h" if h else "", f"{m}m" if m else "", f"{s}s"]
    return " ".join(p for p in parts if p)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=None,
                    help="only audit the last N days (default: all collection)")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"No database at {DB_PATH} — has the poller run?")
        return 1

    conn = sqlite3.connect(DB_PATH)
    where, params = "", []
    if args.days:
        where = "WHERE polled_at >= ?"
        params = [datetime.now().timestamp() - args.days * 86400]

    polls = conn.execute(
        f"SELECT polled_at, status FROM poll_log {where} ORDER BY polled_at", params
    ).fetchall()
    if not polls:
        print("No polls logged yet.")
        return 1

    first, last = polls[0][0], polls[-1][0]
    window = last - first
    ok = [p for p in polls if p[1] == "ok"]
    errors = [p for p in polls if p[1] != "ok"]

    # Expected polls over the window at the configured cadence, inclusive of both ends.
    expected = max(int(window // POLL_INTERVAL_S) + 1, len(ok))  # clamp: seed polls arrive off-grid
    missing = max(0, expected - len(ok))
    gap_pct = (missing / expected * 100) if expected else 0.0

    # Longest silence between consecutive successful polls.
    ts = [p[0] for p in ok]
    longest, longest_at = 0, None
    for a, b in zip(ts, ts[1:]):
        if b - a > longest:
            longest, longest_at = b - a, a

    rows, stations, distinct_polls = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT station_id), COUNT(DISTINCT observed_at) FROM station_status"
    ).fetchone()

    stale = conn.execute(
        """SELECT COUNT(*) FROM station_status
           WHERE observed_at = (SELECT MAX(observed_at) FROM station_status)
             AND last_reported IS NOT NULL
             AND observed_at - last_reported > 3600"""
    ).fetchone()[0]

    db_mb = DB_PATH.stat().st_size / 1e6
    days = window / 86400

    print("Bay Wheels collection health")
    print("=" * 46)
    print(f"  window          {datetime.fromtimestamp(first):%Y-%m-%d %H:%M} "
          f"→ {datetime.fromtimestamp(last):%Y-%m-%d %H:%M}  ({fmt_dur(window)})")
    print(f"  polls ok        {len(ok):,} of {expected:,} expected")
    print(f"  missing         {missing:,}  ({gap_pct:.2f}%)   gate: <{GAP_GATE_PCT}%")
    print(f"  errors logged   {len(errors):,}")
    if longest_at:
        print(f"  longest gap     {fmt_dur(longest)} starting "
              f"{datetime.fromtimestamp(longest_at):%Y-%m-%d %H:%M}")
    print(f"  status rows     {rows:,}   stations {stations}   snapshots {distinct_polls:,}")
    print(f"  stale stations  {stale} reporting >1h old at latest poll")
    print(f"  database        {db_mb:.1f} MB"
          + (f"   (~{db_mb / days:.1f} MB/day)" if days > 0.5 else ""))

    if errors:
        print("\n  recent errors:")
        for polled_at, _ in errors[-5:]:
            err = conn.execute(
                "SELECT error FROM poll_log WHERE polled_at = ?", (polled_at,)
            ).fetchone()[0]
            print(f"    {datetime.fromtimestamp(polled_at):%Y-%m-%d %H:%M}  {err}")

    print()
    if days < GATE_DAYS:
        print(f"  PHASE 1 GATE: not yet — {GATE_DAYS - days:.1f} more days of collection needed.")
        gate_ok = False
    elif gap_pct < GAP_GATE_PCT:
        print(f"  PHASE 1 GATE: PASSED — {days:.1f} days at {gap_pct:.2f}% missing.")
        gate_ok = True
    else:
        print(f"  PHASE 1 GATE: FAILED — {gap_pct:.2f}% missing exceeds {GAP_GATE_PCT}%.")
        print("  Move the poller to an always-on host; a sleeping laptop cannot hold this gate.")
        gate_ok = False

    conn.close()
    return 0 if gate_ok else 2


if __name__ == "__main__":
    sys.exit(main())
