#!/usr/bin/env python3
"""Collection health audit — the gate on Phase 1.

Reads Postgres when DATABASE_URL is set, otherwise the local SQLite database.

The gate is BUCKET COVERAGE, not poll count. What Phase 1 actually needs is that every
5-minute bucket contains at least one observation; how many polls fired is only a proxy,
and an inexact one once the scheduler is best-effort (Vercel cron) rather than
deterministic (cron on a VM). Polling every 2 minutes means a bucket survives a dropped
invocation, which is the whole point of over-scheduling.

Usage:  python3 check_health.py [--days N]
"""

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUCKET_S = 300
COVERAGE_GATE = 98.0     # % of 5-min buckets that must contain an observation
GATE_DAYS = 7


def fmt_dur(seconds):
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    return " ".join(p for p in (f"{d}d" if d else "", f"{h}h" if h else "",
                                f"{m}m" if m else "", f"{s}s") if p)


def load_pg(dsn, since):
    import psycopg
    with psycopg.connect(dsn) as conn:
        where, params = ("WHERE polled_at >= %s", [since]) if since else ("", [])
        polls = [(r[0].timestamp(), r[1], r[2]) for r in conn.execute(
            f"SELECT polled_at, status, error FROM poll_log {where} ORDER BY polled_at",
            params)]
        rows, stations = conn.execute(
            "SELECT count(*), count(DISTINCT station_id) FROM station_status").fetchone()
        size = conn.execute(
            "SELECT pg_database_size(current_database())").fetchone()[0] / 1e6
    return polls, rows, stations, size, "postgres"


def load_sqlite(since):
    import sqlite3
    db = Path(os.environ.get("BW_DB", ROOT / "data" / "baywheels.db"))
    if not db.exists():
        return None
    conn = sqlite3.connect(db)
    where, params = ("WHERE polled_at >= ?", [since.timestamp()]) if since else ("", [])
    polls = [(r[0], r[1], r[2]) for r in conn.execute(
        f"SELECT polled_at, status, error FROM poll_log {where} ORDER BY polled_at", params)]
    rows, stations = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT station_id) FROM station_status").fetchone()
    conn.close()
    return polls, rows, stations, db.stat().st_size / 1e6, "sqlite"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=None)
    args = ap.parse_args()
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)) if args.days else None

    if not (os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")):
        sys.path.insert(0, str(ROOT))
        try:
            from lib.store_pg import _load_dotenv
            _load_dotenv()
        except Exception:
            pass
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    loaded = load_pg(dsn, since) if dsn else load_sqlite(since)
    if not loaded:
        print("No data source found. Set DATABASE_URL or run the local poller first.")
        return 1
    polls, rows, stations, size_mb, backend = loaded
    if not polls:
        print("No polls logged yet.")
        return 1

    ok = [p for p in polls if p[1] == "ok"]
    errors = [p for p in polls if p[1] != "ok"]
    first, last = polls[0][0], polls[-1][0]
    window = last - first
    days = window / 86400

    # Bucket coverage: how much of the timeline is actually observed.
    covered = {int(t // BUCKET_S) for t, _, _ in ok}
    expected = int(window // BUCKET_S) + 1
    coverage = len(covered) / expected * 100 if expected else 0.0

    ts = [p[0] for p in ok]
    longest, longest_at = 0, None
    for a, b in zip(ts, ts[1:]):
        if b - a > longest:
            longest, longest_at = b - a, a

    print(f"Bay Wheels collection health  [{backend}]")
    print("=" * 48)
    print(f"  window          {datetime.fromtimestamp(first):%Y-%m-%d %H:%M} "
          f"→ {datetime.fromtimestamp(last):%Y-%m-%d %H:%M}  ({fmt_dur(window)})")
    print(f"  BUCKET COVERAGE {len(covered):,} of {expected:,} 5-min buckets "
          f"({coverage:.2f}%)   gate: ≥{COVERAGE_GATE}%")
    print(f"  polls ok        {len(ok):,}        errors {len(errors):,}")
    if longest_at:
        print(f"  longest gap     {fmt_dur(longest)} starting "
              f"{datetime.fromtimestamp(longest_at):%Y-%m-%d %H:%M}")
    print(f"  status rows     {rows:,}   stations {stations}")
    print(f"  database        {size_mb:.1f} MB"
          + (f"   (~{size_mb/days:.1f} MB/day)" if days > 0.5 else ""))

    if errors:
        print("\n  recent errors:")
        for t, _, err in errors[-5:]:
            print(f"    {datetime.fromtimestamp(t):%Y-%m-%d %H:%M}  {err}")

    print()
    if days < GATE_DAYS:
        print(f"  PHASE 1 GATE: not yet — {GATE_DAYS - days:.1f} more days needed"
              f" (coverage currently {coverage:.2f}%).")
        return 2
    if coverage >= COVERAGE_GATE:
        print(f"  PHASE 1 GATE: PASSED — {days:.1f} days at {coverage:.2f}% coverage.")
        return 0
    print(f"  PHASE 1 GATE: FAILED — {coverage:.2f}% coverage is below {COVERAGE_GATE}%.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
