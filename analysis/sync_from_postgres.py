#!/usr/bin/env python3
"""Pull collection data from Supabase into the local DuckDB analysis store.

This is the other half of the storage split: Supabase holds the live collection
(because Vercel needs an always-on writable store), and everything gets mirrored here
for analysis. It is also the pressure valve on the 500 MB free tier — once rows are
safely local, old ones can be pruned from Postgres without losing anything.

Incremental: only pulls rows newer than what is already local.
"""

import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import store_pg  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "analysis.duckdb"


def main():
    con = duckdb.connect(str(DB))
    pg = store_pg.connect()

    con.execute("""CREATE TABLE IF NOT EXISTS station_info (
        station_id VARCHAR, fetched_on DATE, name VARCHAR, short_name VARCHAR,
        lat DOUBLE, lon DOUBLE, region_id VARCHAR, capacity INTEGER, address VARCHAR)""")
    con.execute("""CREATE TABLE IF NOT EXISTS station_status (
        station_id VARCHAR, observed_at TIMESTAMPTZ, feed_last_updated TIMESTAMPTZ,
        last_reported TIMESTAMPTZ, num_bikes_available SMALLINT, num_ebikes_available SMALLINT,
        num_bikes_disabled SMALLINT, num_docks_available SMALLINT, num_docks_disabled SMALLINT,
        is_installed SMALLINT, is_renting SMALLINT, is_returning SMALLINT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS poll_log (
        polled_at TIMESTAMPTZ, status VARCHAR, feed_last_updated TIMESTAMPTZ,
        stations_seen INTEGER, rows_written INTEGER, duration_ms INTEGER, error VARCHAR)""")

    for table, tcol in (("station_status", "observed_at"), ("poll_log", "polled_at")):
        hi = con.execute(f"SELECT max({tcol}) FROM {table}").fetchone()[0]
        rows = pg.execute(
            f"SELECT * FROM {table}" + (f" WHERE {tcol} > %s" if hi else ""),
            ([hi] if hi else [])).fetchall()
        if rows:
            con.executemany(
                f"INSERT INTO {table} VALUES ({','.join(['?'] * len(rows[0]))})", rows)
        n = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        print(f"  {table:<16} +{len(rows):>6,} new   ({n:,} total)")

    # station_info is small and slowly-changing: replace wholesale
    rows = pg.execute("SELECT * FROM station_info").fetchall()
    con.execute("DELETE FROM station_info")
    con.executemany(f"INSERT INTO station_info VALUES ({','.join(['?'] * len(rows[0]))})", rows)
    print(f"  {'station_info':<16} {len(rows):>7,} rows (replaced)")

    pg.close()
    print(f"\nlocal db: {DB.stat().st_size/1e6:.0f} MB")
    con.close()


if __name__ == "__main__":
    sys.exit(main())
