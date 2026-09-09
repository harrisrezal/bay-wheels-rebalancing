#!/usr/bin/env python3
"""Phase 3 — would targeted rebalancing have prevented the failures?

Phase 2 established that prediction is not the constraint: at 60 minutes, the horizon
a van could physically meet, the forecast lifts 5.3x over base rate. Meanwhile 91% of
stockouts are never attended by a van at all, and the ones that are wait a median of
6.8 hours. So the open question is not "can we see it coming" but "how much would
acting on it actually recover".

THE POLICY. At each decision epoch, look only at what was knowable then:
  1. rank stations by expected lost trips if they run dry within the window
  2. rank donor stations by stock above their own needs over the same window
  3. assign a fixed number of vans, each with a capacity and a time budget, greedily
     by benefit per minute of travel

Greedy rather than an optimal solve, deliberately. The objective here is legible — an
operator can read the ranking and agree or disagree with it — and the gap to optimal is
smaller than the uncertainty in the demand estimates it is built on.

THE CAVEAT, WHICH IS NOT SMALL. This replays history with moves inserted; it cannot
re-simulate riders. It assumes demand is independent of supply, which is false — more
bikes generate more trips. So results are reported as PREVENTED STOCKOUTS, never as
recovered revenue, and the prevention estimate itself inherits the lower-bound demand
figures from step 4.
"""

import math
import sys
from pathlib import Path

import duckdb
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "analysis.duckdb"

WINDOW_MIN = 60         # how far ahead each decision looks
EPOCH_MIN = 30          # how often a dispatch decision is made
VAN_CAPACITY = 20       # bikes a van carries
LOAD_UNLOAD_MIN = 6     # fixed handling time per stop
VAN_SPEED_KMH = 18      # urban van speed including traffic and parking
SHIFT_MIN = 60          # travel budget per van per epoch


def haversine(a_lat, a_lon, b_lat, b_lon):
    R = 6371.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lon - a_lon)
    h = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(h))


def main():
    con = duckdb.connect(str(DB))

    st = con.execute("""SELECT DISTINCT ON (station_id) station_id, name, lat, lon
                        FROM station_info WHERE lat IS NOT NULL
                        ORDER BY station_id, fetched_on DESC""").fetchall()
    pos = {s[0]: (s[2], s[3]) for s in st}
    name = {s[0]: s[1] for s in st}

    # Test window only — the same held-out period Phase 2 was scored on.
    split = con.execute("""SELECT min(bucket) + (max(bucket)-min(bucket)) * 0.7
                           FROM grid_status""").fetchone()[0]
    rows = con.execute(f"""
        SELECT g.bucket, g.station_id, g.num_ebikes_available AS e,
               g.num_docks_available AS d, d.ebike_demand_per_hour AS dem
        FROM grid_status g
        JOIN demand_baseline d
          ON d.station_id = g.station_id
         AND d.hour = CAST(strftime(g.bucket AT TIME ZONE 'America/Los_Angeles','%H') AS INT)
         AND d.daytype = CASE WHEN dayofweek(g.bucket AT TIME ZONE 'America/Los_Angeles') IN (0,6)
                              THEN 'weekend' ELSE 'weekday' END
        WHERE g.bucket >= '{split}' AND g.is_serving AND NOT d.low_confidence
          AND g.num_bikes_available IS NOT NULL
        ORDER BY g.bucket""").fetchall()

    from collections import defaultdict
    by_t = defaultdict(list)
    for b, s, e, d, dem in rows:
        by_t[b].append((s, e or 0, d or 0, dem or 0.0))
    times = sorted(by_t)
    epochs = times[::EPOCH_MIN // 5]
    print(f"backtest window: {times[0]:%Y-%m-%d %H:%M} → {times[-1]:%Y-%m-%d %H:%M} UTC")
    print(f"  {len(epochs)} decision epochs, one every {EPOCH_MIN} min\n")

    need_per_bike = WINDOW_MIN / 60.0

    print(f"  {'vans':>5} {'moves':>7} {'bikes':>7} {'stockouts prevented':>21} "
          f"{'bikes per prevention':>21}")
    for n_vans in (0, 1, 2, 4, 8):
        prevented = 0
        bikes_moved = 0
        moves = 0
        for t in epochs:
            snap = by_t[t]
            # who will run dry within the window, and how many bikes short
            deficits = []
            donors = []
            for s, e, d, dem in snap:
                expected = dem * need_per_bike
                if expected <= 0.05:
                    continue
                short = expected - e
                if short > 0.5:
                    deficits.append((s, short, expected))
                elif e - expected >= 3:
                    donors.append((s, min(e - expected, e - 1)))
            if not deficits or not donors or n_vans == 0:
                continue
            deficits.sort(key=lambda x: -x[1])
            donors.sort(key=lambda x: -x[1])

            budget = [SHIFT_MIN] * n_vans
            cap = [VAN_CAPACITY] * n_vans
            donor_pool = {s: q for s, q in donors}
            for s, short, expected in deficits:
                placed = False
                for v in range(n_vans):
                    if cap[v] < 1 or budget[v] <= LOAD_UNLOAD_MIN:
                        continue
                    best, bestt = None, None
                    for ds, dq in donor_pool.items():
                        if dq < 1 or ds == s:
                            continue
                        km = haversine(*pos[ds], *pos[s])
                        mins = km / VAN_SPEED_KMH * 60 + LOAD_UNLOAD_MIN
                        if mins <= budget[v] and (bestt is None or mins < bestt):
                            best, bestt = ds, mins
                    if best is None:
                        continue
                    take = int(min(math.ceil(short), donor_pool[best], cap[v]))
                    if take < 1:
                        continue
                    donor_pool[best] -= take
                    cap[v] -= take
                    budget[v] -= bestt
                    bikes_moved += take
                    moves += 1
                    if take >= short:
                        prevented += 1
                    placed = True
                    break
                if not placed:
                    continue
        ratio = bikes_moved / prevented if prevented else float("nan")
        print(f"  {n_vans:>5} {moves:>7,} {bikes_moved:>7,} {prevented:>21,} "
              f"{ratio:>21.1f}")

    print("\n  'stockouts prevented' = predicted shortfalls fully covered by a delivery.")
    print("  Counterfactual: replays history with moves inserted. It cannot re-simulate")
    print("  riders, so this is prevented STOCKOUTS, never recovered revenue.")
    con.close()


if __name__ == "__main__":
    sys.exit(main())
