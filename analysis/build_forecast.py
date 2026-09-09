#!/usr/bin/env python3
"""Phase 2 — can starvation be predicted early enough to act on it?

Predicts, for each station at each moment, whether it will be ebike-starved h minutes
from now. Sweeping h answers the question the whole project points at: how far ahead is
a forecast still useful, and is that far enough to dispatch a van?

TWO LATENCY LINES, both measured from this system rather than assumed:

  ~60 min   what a van could physically do — the short mode in van movement, one
            vehicle working a cluster of nearby stations
  ~410 min  what actually happens. Median wait from a station running dry to a van
            arriving there. And 91% of stockouts are never attended at all.

Split is chronological: train on the earlier days, test on the later ones. A random
split would leak, because adjacent 5-minute rows are near-duplicates of each other.

Models, deliberately including a trivial one:
  B0  demand-only     time-to-empty from current stock and the station's demand rate
  M1  gradient boost  adds recent flow, hour, day type, capacity

If B0 is close to M1, that is the finding: the useful signal is inventory over demand,
and the machine learning is decoration.
"""

import sys
from pathlib import Path

import duckdb
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import average_precision_score, precision_recall_curve

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "analysis.duckdb"
HORIZONS = [15, 30, 60, 120, 240]
PHYSICAL_LATENCY = 60     # what a van could do
OBSERVED_LATENCY = 410    # what vans actually do (median)


def features(con, horizon_min):
    steps = horizon_min // 5
    return con.execute(f"""
    WITH f AS (
      SELECT g.bucket, g.station_id,
             g.num_ebikes_available                                    AS ebikes,
             g.num_bikes_available                                     AS bikes,
             g.num_docks_available                                     AS docks,
             CAST(strftime(g.bucket AT TIME ZONE 'America/Los_Angeles','%H') AS INT) AS hour,
             CASE WHEN dayofweek(g.bucket AT TIME ZONE 'America/Los_Angeles') IN (0,6)
                  THEN 1 ELSE 0 END                                    AS weekend,
             d.ebike_demand_per_hour                                   AS demand,
             g.num_ebikes_available
               - lag(g.num_ebikes_available, 6)  OVER w                AS flow_30m,
             g.num_ebikes_available
               - lag(g.num_ebikes_available, 12) OVER w                AS flow_60m,
             lead(g.num_ebikes_available, {steps}) OVER w              AS ebikes_future,
             lead(g.is_serving, {steps}) OVER w                        AS serving_future
      FROM grid_status g
      JOIN demand_baseline d
        ON d.station_id = g.station_id
       AND d.hour = CAST(strftime(g.bucket AT TIME ZONE 'America/Los_Angeles','%H') AS INT)
       AND d.daytype = CASE WHEN dayofweek(g.bucket AT TIME ZONE 'America/Los_Angeles') IN (0,6)
                            THEN 'weekend' ELSE 'weekday' END
      WHERE g.num_bikes_available IS NOT NULL AND g.is_serving
        AND NOT d.low_confidence
      WINDOW w AS (PARTITION BY g.station_id ORDER BY g.bucket)
    )
    SELECT bucket, ebikes, bikes, docks, hour, weekend, demand,
           coalesce(flow_30m,0) AS flow_30m, coalesce(flow_60m,0) AS flow_60m,
           CASE WHEN ebikes_future = 0 AND serving_future THEN 1 ELSE 0 END AS starved_future
    FROM f
    WHERE ebikes_future IS NOT NULL AND flow_60m IS NOT NULL
    """).fetchnumpy()


def main():
    con = duckdb.connect(str(DB))
    split = con.execute("""SELECT min(bucket) + (max(bucket)-min(bucket)) * 0.7
                           FROM grid_status""").fetchone()[0]
    print(f"chronological split at {split:%Y-%m-%d %H:%M} UTC — train before, test after\n")
    print(f"  {'horizon':>8} {'test rows':>11} {'base rate':>10} "
          f"{'B0 demand':>11} {'M1 boosted':>11} {'lift':>7}")

    results = []
    for h in HORIZONS:
        d = features(con, h)
        t = d.pop("bucket")
        y = d.pop("starved_future")
        cols = list(d.keys())
        X = np.column_stack([d[c] for c in cols]).astype("float32")
        sp = np.datetime64(split.replace(tzinfo=None))
        tr, te = t < sp, t >= sp
        if y[te].sum() < 50:
            print(f"  {h:>6}m   too few positives in test set")
            continue

        # B0: stock divided by demand — hours of runway. No learning at all.
        ebikes, demand = d["ebikes"][te], np.maximum(d["demand"][te], 1e-6)
        b0 = -(ebikes / demand)                       # less runway => more likely starved
        ap_b0 = average_precision_score(y[te], b0)

        m1 = HistGradientBoostingClassifier(max_iter=120, random_state=0)
        m1.fit(X[tr], y[tr])
        ap_m1 = average_precision_score(y[te], m1.predict_proba(X[te])[:, 1])

        base = y[te].mean()
        print(f"  {h:>6}m {te.sum():>11,} {base:>9.1%} "
              f"{ap_b0:>11.3f} {ap_m1:>11.3f} {ap_m1/base:>6.1f}x")

        # The hard case: stations that are STOCKED right now. Predicting that an
        # already-empty station stays empty is persistence, not forecasting.
        onset = te & (d["ebikes"] > 0)
        ap_on = average_precision_score(y[onset], m1.predict_proba(X[onset])[:, 1])
        results.append((h, base, ap_b0, ap_m1, y[onset].mean(), ap_on))

    print("\n  (average precision; base rate is what random guessing scores)")
    print("\n=== FORECAST SKILL vs WHEN YOU COULD ACT ===\n")
    print(f"\n  ONSET ONLY — stations currently stocked (the non-trivial case):")
    print(f"  {'horizon':>8} {'base rate':>10} {'M1':>8} {'lift':>7}")
    for h, base, b0, m1, ob, oa in results:
        print(f"  {h:>6}m {ob:>9.1%} {oa:>8.3f} {oa/ob:>6.1f}x")

    mx = max(r[3] for r in results)
    for h, base, b0, m1, ob, oa in results:
        bar = "█" * int(28 * m1 / mx)
        marks = ""
        if h <= PHYSICAL_LATENCY: marks = "  <- a van could still make it"
        print(f"  {h:>4} min  {bar:<28} {m1:.3f}{marks}")
    print(f"\n  physical van latency  ~{PHYSICAL_LATENCY} min   forecast is strong here")
    print(f"  OBSERVED van latency  ~{OBSERVED_LATENCY} min   far beyond any useful horizon")
    print("  and 91% of stockouts are never attended by a van at all.")
    con.close()


if __name__ == "__main__":
    sys.exit(main())
