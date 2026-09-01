#!/usr/bin/env python3
"""Download and load Bay Wheels historical trip CSVs into the local DuckDB store.

Trips live LOCALLY, not in Supabase: ~1.76M rows for three months would be ~316 MB,
63% of the 500 MB free tier, collapsing collection runway from ~7 weeks to ~2.5.
Supabase holds live collection only, because that is the sole thing needing an
always-on writable store. Analysis is batch work on this machine.

Usage:
  python3 analysis/ingest_trips.py 202605 202606 202607
  python3 analysis/ingest_trips.py --last 3
"""

import argparse
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "trips_raw"
DB = ROOT / "data" / "analysis.duckdb"
BUCKET = "https://s3.amazonaws.com/baywheels-data/"
UA = "bay-wheels-rebalancing/0.3 (personal research project)"

DDL = """
CREATE TABLE IF NOT EXISTS trips (
    ride_id            VARCHAR,
    rideable_type      VARCHAR,
    started_at         TIMESTAMP,
    ended_at           TIMESTAMP,
    start_station_name VARCHAR,
    start_station_id   VARCHAR,   -- maps to station_info.short_name, NOT station_id
    end_station_name   VARCHAR,
    end_station_id     VARCHAR,
    start_lat          DOUBLE,
    start_lng          DOUBLE,
    end_lat            DOUBLE,
    end_lng            DOUBLE,
    member_casual      VARCHAR,
    source_month       VARCHAR
);
"""


def candidates(month):
    """Naming is inconsistent across months: some are .csv.zip, some .zip."""
    return [f"{month}-baywheels-tripdata.csv.zip", f"{month}-baywheels-tripdata.zip"]


def download(month):
    out = RAW / f"{month}.csv"
    if out.exists():
        print(f"  {month}: already extracted ({out.stat().st_size/1e6:.0f} MB)")
        return out
    for name in candidates(month):
        try:
            req = urllib.request.Request(BUCKET + name, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=180) as r:
                blob = r.read()
        except Exception:
            continue
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            member = next(n for n in z.namelist()
                          if n.endswith(".csv") and "MACOSX" not in n)
            out.write_bytes(z.read(member))
        print(f"  {month}: downloaded {len(blob)/1e6:.0f} MB → {out.stat().st_size/1e6:.0f} MB CSV")
        return out
    print(f"  {month}: NOT FOUND (tried {', '.join(candidates(month))})")
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("months", nargs="*", help="e.g. 202605 202606 202607")
    ap.add_argument("--last", type=int, help="most recent N months before today")
    args = ap.parse_args()

    months = args.months
    if args.last:
        from datetime import date
        d = date.today().replace(day=1)
        months = []
        for _ in range(args.last):
            d = (d.replace(day=1) - __import__("datetime").timedelta(days=1)).replace(day=1)
            months.append(d.strftime("%Y%m"))
        months.reverse()
    if not months:
        ap.error("give months or --last N")

    RAW.mkdir(parents=True, exist_ok=True)
    print(f"months: {', '.join(months)}\n")
    files = [(m, download(m)) for m in months]

    con = duckdb.connect(str(DB))
    con.execute(DDL)
    for month, path in files:
        if not path:
            continue
        already = con.execute("SELECT count(*) FROM trips WHERE source_month = ?",
                              [month]).fetchone()[0]
        if already:
            print(f"  {month}: {already:,} rows already loaded, skipping")
            continue
        con.execute("""
            INSERT INTO trips SELECT
                ride_id, rideable_type, started_at, ended_at,
                start_station_name, start_station_id, end_station_name, end_station_id,
                start_lat, start_lng, end_lat, end_lng, member_casual, ?
            FROM read_csv(?, header=true, timestampformat='%Y-%m-%d %H:%M:%S.%f',
                          types={'start_station_id':'VARCHAR','end_station_id':'VARCHAR',
                                 'ride_id':'VARCHAR'})
        """, [month, str(path)])
        n = con.execute("SELECT count(*) FROM trips WHERE source_month = ?", [month]).fetchone()[0]
        print(f"  {month}: loaded {n:,} rows")

    total = con.execute("SELECT count(*) FROM trips").fetchone()[0]
    print(f"\ntrips table: {total:,} rows   db {DB.stat().st_size/1e6:.0f} MB")
    con.close()


if __name__ == "__main__":
    sys.exit(main())
