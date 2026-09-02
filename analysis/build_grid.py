#!/usr/bin/env python3
"""Build grid_status — the canonical 5-minute observation grid.

Everything downstream aggregates over THIS, never over station_status directly.
station_status is change-data-capture, so it only contains rows for stations that
changed: busy stations appear far more often than quiet ones. Any average taken over it
is silently weighted by activity. The grid restores one row per station per bucket, so
statistics mean what they look like they mean.

Staleness rule, corrected for CDC:
  A bucket is UNKNOWN when *polling* stopped, not when a station stopped changing.
  Under CDC an absent row means "no change", which is real information — a station can
  legitimately hold one state for hours. So the cap is applied to gaps in poll_log
  coverage, not to the age of the last station_status row. Applying it to row age (as
  originally specced, when every row was stored) would wrongly mark quiet stations
  unknown and delete exactly the idle periods starvation analysis depends on.
"""

import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "analysis.duckdb"
BUCKET_MIN = 5
COVERAGE_CAP_MIN = 15   # poll-coverage gap beyond which buckets are unknown


def main():
    con = duckdb.connect(str(DB))
    con.execute("DROP TABLE IF EXISTS grid_status")
    con.execute(f"""
    CREATE TABLE grid_status AS
    WITH bounds AS (
        SELECT time_bucket(INTERVAL '{BUCKET_MIN} min', min(polled_at)) AS lo,
               max(polled_at) AS hi
        FROM poll_log WHERE status = 'ok'
    ),
    buckets AS (
        SELECT unnest(generate_series(lo, hi, INTERVAL '{BUCKET_MIN} min')) AS bucket
        FROM bounds
    ),
    -- A bucket is covered if a successful poll landed in it, OR if the nearest
    -- successful poll is within the coverage cap (tolerates one dropped invocation).
    covered AS (
        SELECT b.bucket,
               (SELECT min(abs(epoch(p.polled_at - b.bucket)))
                FROM poll_log p WHERE p.status = 'ok') IS NOT NULL
               AND (SELECT min(abs(epoch(p.polled_at - b.bucket)))
                    FROM poll_log p WHERE p.status = 'ok'
                      AND p.polled_at BETWEEN b.bucket - INTERVAL '{COVERAGE_CAP_MIN} min'
                                          AND b.bucket + INTERVAL '{COVERAGE_CAP_MIN} min'
                   ) IS NOT NULL AS observed
        FROM buckets b
    ),
    stations AS (SELECT DISTINCT station_id FROM station_status)
    SELECT
        g.bucket,
        g.station_id,
        s.observed_at            AS state_as_of,
        s.num_bikes_available,
        s.num_ebikes_available,
        s.num_bikes_available - s.num_ebikes_available AS classic_available,
        s.num_bikes_disabled,
        s.num_docks_available,
        s.num_docks_disabled,
        s.is_installed, s.is_renting, s.is_returning,
        s.last_reported,
        -- Staleness is measured against WHEN WE OBSERVED the state, not against the
        -- bucket we are filling forward into. A carried-forward cell is not evidence
        -- that the station stopped reporting; whether our coverage is old is a separate
        -- question, already handled by the poll-coverage cap. Comparing to the bucket
        -- made every station look stale after any polling gap, which silently zeroed
        -- is_serving system-wide and made starvation counts drop to 0.
        epoch(s.observed_at - s.last_reported) > 3600                AS is_stale,
        (s.is_installed = 1 AND s.is_renting = 1
         AND NOT (epoch(s.observed_at - s.last_reported) > 3600))    AS is_serving
    FROM (SELECT bucket, station_id FROM covered CROSS JOIN stations WHERE observed) g
    ASOF LEFT JOIN station_status s
      ON s.station_id = g.station_id AND s.observed_at <= g.bucket
    """)

    n, b, st = con.execute(
        "SELECT count(*), count(DISTINCT bucket), count(DISTINCT station_id) FROM grid_status"
    ).fetchone()
    null_state = con.execute(
        "SELECT count(*) FROM grid_status WHERE num_bikes_available IS NULL").fetchone()[0]
    # cast to text in SQL: returning TIMESTAMPTZ to Python would pull in pytz
    lo, hi = con.execute("SELECT strftime(min(bucket), '%Y-%m-%d %H:%M'), strftime(max(bucket), '%Y-%m-%d %H:%M') FROM grid_status").fetchone()

    print(f"grid_status built")
    print(f"  window        {lo} → {hi}  (UTC)")
    print(f"  rows          {n:,}   ({b:,} buckets x {st} stations)")
    print(f"  no state yet  {null_state:,}  (before a station's first observation)")

    print(f"\n  forward-fill age distribution (bucket - state_as_of):")
    for lbl, lo_s, hi_s in [("same bucket", 0, 300), ("5-15 min", 300, 900),
                            ("15-60 min", 900, 3600), ("> 1 hour", 3600, 10**9)]:
        c = con.execute(f"""SELECT count(*) FROM grid_status WHERE state_as_of IS NOT NULL
                            AND epoch(bucket - state_as_of) >= {lo_s}
                            AND epoch(bucket - state_as_of) < {hi_s}""").fetchone()[0]
        print(f"    {lbl:<14} {c:>8,}  ({c/n:>5.1%})")
    con.close()


if __name__ == "__main__":
    sys.exit(main())
