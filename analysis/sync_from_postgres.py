#!/usr/bin/env python3
"""Mirror collection data from Supabase into the local DuckDB analysis store.

Supabase holds the live collection because Vercel needs an always-on writable store.
Everything is mirrored here for analysis, which is also the pressure valve on the
500 MB free tier: once rows are safely local, old ones can be pruned from Postgres.

Uses DuckDB's postgres extension to pull rows directly rather than round-tripping
them through Python. The previous executemany version took over two minutes for a
week of collection and got linearly worse; this reads server-side in one statement.
"""

import os
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.store_pg import _load_dotenv  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "analysis.duckdb"

DDL = {
    "station_info": """CREATE TABLE IF NOT EXISTS station_info (
        station_id VARCHAR, fetched_on DATE, name VARCHAR, short_name VARCHAR,
        lat DOUBLE, lon DOUBLE, region_id VARCHAR, capacity INTEGER, address VARCHAR)""",
    "station_status": """CREATE TABLE IF NOT EXISTS station_status (
        station_id VARCHAR, observed_at TIMESTAMPTZ, feed_last_updated TIMESTAMPTZ,
        last_reported TIMESTAMPTZ, num_bikes_available SMALLINT,
        num_ebikes_available SMALLINT, num_bikes_disabled SMALLINT,
        num_docks_available SMALLINT, num_docks_disabled SMALLINT,
        is_installed SMALLINT, is_renting SMALLINT, is_returning SMALLINT)""",
    "poll_log": """CREATE TABLE IF NOT EXISTS poll_log (
        polled_at TIMESTAMPTZ, status VARCHAR, feed_last_updated TIMESTAMPTZ,
        stations_seen INTEGER, rows_written INTEGER, duration_ms INTEGER, error VARCHAR)""",
}


def main():
    _load_dotenv()
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    if not dsn:
        print("DATABASE_URL is not set (and no .env found).")
        return 1

    con = duckdb.connect(str(DB))
    for ddl in DDL.values():
        con.execute(ddl)

    con.execute("INSTALL postgres; LOAD postgres;")
    # ATTACH does not accept bind parameters, so the DSN is inlined. It carries the
    # database password, so it is escaped for SQL quoting and never printed or logged.
    con.execute(f"ATTACH '{dsn.replace(chr(39), chr(39)*2)}' AS pg (TYPE postgres, READ_ONLY)")
    try:
        for table, tcol in (("station_status", "observed_at"), ("poll_log", "polled_at")):
            hi = con.execute(f"SELECT max({tcol}) FROM {table}").fetchone()[0]
            before = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            if hi:
                con.execute(f"INSERT INTO {table} SELECT * FROM pg.{table} WHERE {tcol} > ?", [hi])
            else:
                con.execute(f"INSERT INTO {table} SELECT * FROM pg.{table}")
            after = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            print(f"  {table:<16} +{after-before:>7,} new   ({after:,} total)")

        # small and slowly-changing: replace wholesale
        con.execute("DELETE FROM station_info")
        con.execute("INSERT INTO station_info SELECT * FROM pg.station_info")
        n = con.execute("SELECT count(*) FROM station_info").fetchone()[0]
        print(f"  {'station_info':<16} {n:>8,} rows (replaced)")
    finally:
        con.execute("DETACH pg")
        con.close()

    print(f"\nlocal db: {DB.stat().st_size/1e6:.0f} MB")


if __name__ == "__main__":
    sys.exit(main())
