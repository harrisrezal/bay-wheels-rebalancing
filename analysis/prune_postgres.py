#!/usr/bin/env python3
"""Delete old collection rows from Supabase once they are safely mirrored locally.

Postgres is the write target because Vercel needs an always-on store. It is not the
archive: DuckDB holds the full history and every analysis reads from there. So Postgres
only needs a recent working window, and on the 500 MB free tier it fills around
2026-10-28 at the current 8.4 MB/day.

SAFETY. Collected history is unrecoverable — GBFS publishes a live snapshot with no
history endpoint, so a row deleted before it is mirrored is gone permanently. This
script therefore refuses to delete anything unless, for every day it is about to touch,
the local DuckDB row count matches Postgres exactly. A mismatch aborts the whole run
rather than deleting the days that happen to agree.

Default is a dry run. Deleting requires --apply.

  python3 analysis/prune_postgres.py                  # report only
  python3 analysis/prune_postgres.py --keep-days 10   # preview a different window
  python3 analysis/prune_postgres.py --apply
"""

import argparse
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import store_pg  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "analysis.duckdb"
KEEP_DAYS = 14          # working window Postgres retains


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-days", type=int, default=KEEP_DAYS)
    ap.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    a = ap.parse_args()

    pg = store_pg.connect()
    con = duckdb.connect(str(DB), read_only=True)

    cutoff = pg.execute(
        "SELECT max(observed_at) - make_interval(days => %s) FROM station_status", [a.keep_days]
    ).fetchone()[0]
    total, oldest = pg.execute(
        "SELECT count(*), min(observed_at) FROM station_status").fetchone()
    doomed = pg.execute(
        "SELECT count(*) FROM station_status WHERE observed_at < %s", [cutoff]).fetchone()[0]

    print(f"keep window     {a.keep_days} days (everything from {cutoff:%Y-%m-%d %H:%M} onward)")
    print(f"postgres rows   {total:,}  from {oldest:%Y-%m-%d %H:%M}")
    print(f"would delete    {doomed:,}  ({doomed/total:.0%})\n")
    if doomed == 0:
        print("nothing older than the window. Nothing to do.")
        return 0

    # Per-day comparison. A single global count could match by coincidence while
    # individual days are missing locally.
    #
    # Both sides are pinned to UTC. Postgres sessions here run UTC while DuckDB uses
    # America/Los_Angeles, and bucketing in different zones shifts rows across the
    # midnight boundary — which made every day look mismatched on the first run even
    # though the totals agreed exactly.
    pg_days = dict(pg.execute(
        """SELECT (observed_at AT TIME ZONE 'UTC')::date, count(*) FROM station_status
           WHERE observed_at < %s GROUP BY 1 ORDER BY 1""", [cutoff]).fetchall())
    duck_days = dict(con.execute(
        """SELECT CAST(observed_at AT TIME ZONE 'UTC' AS DATE) d, count(*) FROM station_status
           WHERE observed_at < ? GROUP BY 1 ORDER BY 1""", [cutoff]).fetchall())

    print(f"  {'day':<12} {'postgres':>10} {'duckdb':>10}  state")
    bad = []
    for day in sorted(pg_days):
        p, d = pg_days[day], duck_days.get(day, 0)
        ok = d >= p
        if not ok:
            bad.append((day, p, d))
        print(f"  {str(day):<12} {p:>10,} {d:>10,}  {'ok' if ok else 'MISSING LOCALLY'}")

    if bad:
        print(f"\n  ABORT: {len(bad)} day(s) are not fully mirrored in DuckDB.")
        print("  Run analysis/sync_from_postgres.py, then try again. Nothing was deleted.")
        return 2

    print(f"\n  all {len(pg_days)} days verified present locally")
    if not a.apply:
        print("  dry run. Re-run with --apply to delete.")
        return 0

    before = pg.execute(
        """SELECT pg_total_relation_size('public.station_status')::bigint"""
    ).fetchone()[0]
    pg.execute("DELETE FROM station_status WHERE observed_at < %s", [cutoff])
    pg.execute("VACUUM (ANALYZE) station_status")
    after = pg.execute(
        """SELECT pg_total_relation_size('public.station_status')::bigint"""
    ).fetchone()[0]

    print(f"\n  deleted {doomed:,} rows")
    print(f"  table {before/1e6:.1f} MB -> {after/1e6:.1f} MB")
    print("\n  Note: plain VACUUM frees the space for Postgres to reuse but does not")
    print("  return it to the filesystem, so the reported size may barely move. What it")
    print("  guarantees is that the table stops GROWING, which is what the quota needs.")
    print("  VACUUM FULL would shrink it on disk but takes an exclusive lock and needs")
    print("  temporary space for a full rewrite; not worth it at this size.")

    pg.close(); con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
