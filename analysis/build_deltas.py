#!/usr/bin/env python3
"""Build station_deltas and infer rebalancing-truck events (spec F3).

Deltas are computed over grid_status, not station_status, so every interval is the same
length and comparable. Under CDC a bucket where nothing changed yields a zero delta,
which is correct — the grid already distinguishes "no change" from "not observed".

Truck inference is the point of this step. A jump of >= TRUCK_MIN bikes at one station
within a single 5-minute bucket is a van, not that many simultaneous riders. This
reverse-engineers the operator's actual rebalancing behaviour from public data, which
gives Phase 3 a real-world comparison baseline ("my policy vs what Lyft actually did")
instead of a hypothetical one ("my policy vs doing nothing").

It also grounds Phase 2's action-latency constant empirically: the gap between
consecutive truck events at different stations is an observed operator response time,
rather than a guess at van speed.
"""

import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "analysis.duckdb"
TRUCK_MIN = 5      # bikes moved in one 5-min bucket to call it a van


def main():
    con = duckdb.connect(str(DB))
    con.execute("DROP TABLE IF EXISTS station_deltas")
    con.execute(f"""
    CREATE TABLE station_deltas AS
    WITH seq AS (
        SELECT bucket, station_id, state_as_of,
               num_bikes_available  AS bikes,
               num_ebikes_available AS ebikes,
               classic_available    AS classic,
               num_docks_available  AS docks,
               num_bikes_disabled   AS disabled,
               lag(num_bikes_available)  OVER w AS p_bikes,
               lag(num_ebikes_available) OVER w AS p_ebikes,
               lag(classic_available)    OVER w AS p_classic,
               lag(num_docks_available)  OVER w AS p_docks,
               lag(num_bikes_disabled)   OVER w AS p_disabled,
               lag(state_as_of)          OVER w AS p_state_as_of
        FROM grid_status
        WHERE num_bikes_available IS NOT NULL
        WINDOW w AS (PARTITION BY station_id ORDER BY bucket)
    )
    SELECT bucket, station_id,
           bikes - p_bikes       AS d_bikes,
           ebikes - p_ebikes     AS d_ebikes,
           classic - p_classic   AS d_classic,
           docks - p_docks       AS d_docks,
           disabled - p_disabled AS d_disabled,
           bikes, docks,
           -- state_as_of unchanged => the grid carried the same row forward, so this
           -- bucket contains no new observation at all, not merely "no movement".
           (state_as_of = p_state_as_of) AS carried_forward,
           -- Conservation test: a bike physically moving must free or consume a dock,
           -- so d_bikes + d_docks ~ 0. Without this, a station briefly reporting zero
           -- (feed artifact, sometimes with is_renting=0) is indistinguishable from a
           -- van removing every bike. Measured on real data: 19% of naive candidates
           -- were artifacts, and only 3 of 15 had is_renting=0, so a service-flag check
           -- alone would not have caught them.
           CASE
             WHEN state_as_of = p_state_as_of                     THEN 'no_observation'
             WHEN abs((bikes - p_bikes) + (docks - p_docks)) > 1
                  AND abs(bikes - p_bikes) >= {TRUCK_MIN}         THEN 'feed_artifact'
             WHEN bikes - p_bikes >=  {TRUCK_MIN}                 THEN 'inferred_truck_dropoff'
             WHEN bikes - p_bikes <= -{TRUCK_MIN}                 THEN 'inferred_truck_pickup'
             WHEN bikes - p_bikes <> 0                            THEN 'rider_churn'
             ELSE 'idle'
           END AS event_class
    FROM seq
    WHERE p_bikes IS NOT NULL
    """)

    n = con.execute("SELECT count(*) FROM station_deltas").fetchone()[0]
    print(f"station_deltas: {n:,} rows\n")

    print(f"{'event_class':<24} {'count':>8} {'share':>7} {'bikes moved':>12}")
    for c, k, mv in con.execute("""
        SELECT event_class, count(*), sum(abs(d_bikes))
        FROM station_deltas GROUP BY 1 ORDER BY 2 DESC""").fetchall():
        print(f"  {c:<22} {k:>8,} {k/n:>6.1%} {mv or 0:>12,}")

    print("\n--- inferred truck events ---")
    t = con.execute("""
        SELECT count(*), count(DISTINCT station_id), sum(abs(d_bikes))
        FROM station_deltas WHERE event_class LIKE 'inferred_truck%'""").fetchone()
    print(f"  events {t[0]}   distinct stations {t[1]}   bikes moved {t[2] or 0}")
    if t[0]:
        print(f"\n  {'time':<7} {'station':<34} {'d_bikes':>8} {'kind':>10}")
        for b, name, d, k in con.execute("""
            SELECT strftime(sd.bucket,'%H:%M'), i.name, sd.d_bikes,
                   CASE WHEN sd.d_bikes>0 THEN 'dropoff' ELSE 'pickup' END
            FROM station_deltas sd
            JOIN (SELECT DISTINCT ON(station_id) station_id,name FROM station_info
                  ORDER BY station_id, fetched_on DESC) i USING (station_id)
            WHERE sd.event_class LIKE 'inferred_truck%'
            ORDER BY sd.bucket LIMIT 15""").fetchall():
            print(f"  {b:<7} {name[:34]:<34} {d:>+8} {k:>10}")

    print(f"\n--- threshold sensitivity (conservation-filtered) ---")
    for th in (3, 4, 5, 6, 8, 10):
        c = con.execute(f"""SELECT count(*) FROM station_deltas
            WHERE NOT carried_forward AND abs(d_bikes) >= {th}
              AND abs(d_bikes + d_docks) <= 1""").fetchone()[0]
        print(f"  >= {th:>2} bikes/5min : {c:>5} events")

    print("\n--- observed operator response latency (Phase 2 action-latency input) ---")
    gaps = con.execute("""
        WITH t AS (SELECT bucket, station_id,
                          lag(bucket) OVER (ORDER BY bucket) prev
                   FROM station_deltas WHERE event_class LIKE 'inferred_truck%')
        SELECT CAST(epoch(bucket - prev)/60 AS INT) g FROM t WHERE prev IS NOT NULL
          AND epoch(bucket - prev) > 0""").fetchall()
    if gaps:
        v = sorted(x[0] for x in gaps)
        print(f"  gaps between consecutive truck events: n={len(v)}")
        print(f"  min {v[0]}m   median {v[len(v)//2]}m   p90 {v[int(len(v)*0.9)]}m   max {v[-1]}m")
    con.close()


if __name__ == "__main__":
    sys.exit(main())
