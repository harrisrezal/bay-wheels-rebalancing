#!/usr/bin/env python3
"""Migrate locally-collected SQLite data into Postgres, converting to CDC form.

The local poller stored every row; Postgres stores only changes. This replays the
SQLite series per station and keeps a row only where the inventory state differs from
that station's previous observation — the same rule the live poller applies.

Usage:
  python3 backfill_sqlite_to_pg.py --dry-run     # report only, touches nothing
  python3 backfill_sqlite_to_pg.py               # requires DATABASE_URL
"""

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import gbfs  # noqa: E402

ROOT = Path(__file__).resolve().parent
SQLITE_DB = ROOT / "data" / "baywheels.db"
F = gbfs.STATUS_FIELDS


def ts(unix):
    return datetime.fromtimestamp(unix, timezone.utc) if unix else None


def build():
    """Returns (status_rows, current_rows, info_rows, poll_rows, total_seen)."""
    sq = sqlite3.connect(SQLITE_DB)

    info = [
        (r[0], r[1], *r[2:])
        for r in sq.execute(
            "SELECT station_id, fetched_on, name, short_name, lat, lon,"
            " region_id, capacity, address FROM station_info"
        )
    ]
    polls = [
        (ts(r[0]), r[1], ts(r[2]), r[3], r[4], r[5], r[6])
        for r in sq.execute(
            "SELECT polled_at, status, feed_last_updated, stations_seen,"
            " rows_written, duration_ms, error FROM poll_log"
        )
    ]

    status, last_state, latest = [], {}, {}
    total = 0
    for row in sq.execute(
        f"SELECT station_id, observed_at, feed_last_updated, last_reported, {', '.join(F)}"
        f" FROM station_status ORDER BY station_id, observed_at"
    ):
        total += 1
        sid, obs, flu, lr = row[0], row[1], row[2], row[3]
        state = tuple(row[4:])
        if last_state.get(sid) == state:
            continue                      # unchanged — CDC drops it
        last_state[sid] = state
        status.append((sid, ts(obs), ts(flu), ts(lr), *state))
        latest[sid] = (sid, ts(obs), *state)

    sq.close()
    return status, list(latest.values()), info, polls, total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not SQLITE_DB.exists():
        print(f"No SQLite database at {SQLITE_DB}")
        return 1

    status, current, info, polls, total = build()
    kept = len(status)
    print(f"SQLite rows read      {total:,}")
    print(f"CDC rows to migrate   {kept:,}  ({kept/total:.1%} of source)")
    print(f"  dropped as unchanged {total-kept:,}")
    print(f"station_current rows  {len(current):,}")
    print(f"station_info rows     {len(info):,}")
    print(f"poll_log rows         {len(polls):,}")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    from lib import store_pg
    conn = store_pg.connect()
    try:
        store_pg.ensure_schema(conn)
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO station_info (station_id, fetched_on, name, short_name,"
                " lat, lon, region_id, capacity, address) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (station_id, fetched_on) DO NOTHING", info)
            cur.executemany(
                "INSERT INTO poll_log (polled_at, status, feed_last_updated, stations_seen,"
                " rows_written, duration_ms, error) VALUES (%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (polled_at) DO NOTHING", polls)
            cur.executemany(
                f"INSERT INTO station_status (station_id, observed_at, feed_last_updated,"
                f" last_reported, {', '.join(F)})"
                f" VALUES ({', '.join(['%s'] * (4 + len(F)))})"
                f" ON CONFLICT (station_id, observed_at) DO NOTHING", status)
            cur.executemany(
                f"INSERT INTO station_current (station_id, observed_at, {', '.join(F)})"
                f" VALUES ({', '.join(['%s'] * (2 + len(F)))})"
                f" ON CONFLICT (station_id) DO UPDATE SET observed_at = EXCLUDED.observed_at,"
                + ", ".join(f" {f} = EXCLUDED.{f}" for f in F), current)
    finally:
        conn.close()

    print("\nMigrated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
