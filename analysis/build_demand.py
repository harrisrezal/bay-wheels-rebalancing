#!/usr/bin/env python3
"""Build demand_baseline — estimated true demand, corrected for censoring.

THE PROBLEM. A station with no ebikes records zero departures, but that is not zero
demand: it is demand we cannot see. Trip counts are a floor, not a measurement. Left
uncorrected the bias points the wrong way — the stations that fail most look like the
ones wanted least — which the literature calls a self-reinforcing vicious cycle.

THE METHOD. Poisson rate with availability as an exposure offset:

    observed_departures ~ Poisson( lambda * available_buckets )
    lambda_hat          = observed_departures / available_buckets

Only buckets where the station actually had stock contribute exposure, so the rate is
estimated from uncensored time alone and then applied to the whole period. That is the
maximum-likelihood estimate under a Poisson exposure model, and it is what Yin, Zheng &
Kong (2026) point to for partial censoring: "given an availability fraction, [the
censored likelihood] could be replaced by an exposure-aware (binomial-thinning or
interval-censoring) likelihood."

TWO HONEST LIMITS, both recorded in the output.

1. These are LOWER BOUNDS. The proper recovery is E[D | D >= observed] from a fitted
   censored likelihood. This estimator instead assumes demand during a stockout equals
   demand while stock lasted. It does not: a station empties precisely BECAUSE demand
   was high, so the true rate during the gap is higher than the rate before it. The
   same paper measured naive handling running about 8% low.

2. GRAIN IS DAYTYPE, NOT WEEKDAY. One week of collection gives exactly one Tuesday, so
   a station x weekday x hour cell has n=1 and is far too noisy to divide by. Pooling
   into weekday/weekend gives n=5 and n=2. Revisit once several weeks have accumulated.

Cells are shrunk toward the station's own overall rate (Gamma-Poisson conjugate), so a
cell with almost no exposure cannot produce a wild rate from one lucky observation.
"""

import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "analysis.duckdb"
BUCKETS_PER_HOUR = 12          # 5-minute grid
SHRINK_PSEUDO_BUCKETS = 12     # 1 hour of prior exposure
MIN_AVAILABILITY = 0.15        # below this, extrapolation is not credible
MIN_EXPOSED_BUCKETS = 6        # 30 min of stocked time across the whole window


def main():
    con = duckdb.connect(str(DB))
    con.execute("DROP TABLE IF EXISTS demand_baseline")
    con.execute(f"""
    CREATE TABLE demand_baseline AS
    WITH obs AS (
        SELECT
            g.station_id,
            g.bucket,
            CASE WHEN dayofweek(g.bucket AT TIME ZONE 'America/Los_Angeles') IN (0, 6)
                 THEN 'weekend' ELSE 'weekday' END                              AS daytype,
            CAST(strftime(g.bucket AT TIME ZONE 'America/Los_Angeles', '%H') AS INT) AS hour,
            -- Exposure is the state at the START of the interval: you can only take a
            -- bike that was already there. Using the end state would credit exposure to
            -- the very bucket in which the station ran dry.
            lag(g.num_ebikes_available) OVER w > 0 AND lag(g.is_serving) OVER w   AS ebike_exposed,
            lag(g.classic_available)    OVER w > 0 AND lag(g.is_serving) OVER w   AS classic_exposed,
            -- Rider departures only. Truck pickups are large deliberate removals and
            -- would otherwise be counted as demand.
            CASE WHEN d.event_class = 'rider_churn' AND d.d_ebikes  < 0 THEN -d.d_ebikes  ELSE 0 END AS ebike_dep,
            CASE WHEN d.event_class = 'rider_churn' AND d.d_classic < 0 THEN -d.d_classic ELSE 0 END AS classic_dep
        FROM grid_status g
        LEFT JOIN station_deltas d USING (station_id, bucket)
        WHERE g.num_bikes_available IS NOT NULL
        WINDOW w AS (PARTITION BY g.station_id ORDER BY g.bucket)
    ),
    cells AS (
        SELECT station_id, daytype, hour,
               count(*)                                             AS total_buckets,
               count(*) FILTER (WHERE ebike_exposed)                 AS ebike_exposed_buckets,
               count(*) FILTER (WHERE classic_exposed)               AS classic_exposed_buckets,
               sum(ebike_dep)   FILTER (WHERE ebike_exposed)         AS ebike_obs,
               sum(classic_dep) FILTER (WHERE classic_exposed)       AS classic_obs
        FROM obs WHERE ebike_exposed IS NOT NULL GROUP BY 1,2,3
    ),
    station_prior AS (   -- the station's own overall rate, used to shrink thin cells
        SELECT station_id,
               sum(ebike_obs)   / nullif(sum(ebike_exposed_buckets),   0) AS ebike_prior,
               sum(classic_obs) / nullif(sum(classic_exposed_buckets), 0) AS classic_prior
        FROM cells GROUP BY 1
    )
    SELECT
        c.station_id, c.daytype, c.hour,
        c.total_buckets, c.ebike_exposed_buckets, c.ebike_obs,
        c.ebike_exposed_buckets   * 1.0 / c.total_buckets                  AS ebike_availability,
        c.classic_exposed_buckets * 1.0 / c.total_buckets                  AS classic_availability,

        -- Gamma-Poisson shrinkage toward the station's own rate.
        (c.ebike_obs   + {SHRINK_PSEUDO_BUCKETS} * coalesce(p.ebike_prior, 0))
          / (c.ebike_exposed_buckets   + {SHRINK_PSEUDO_BUCKETS})          AS ebike_rate_per_bucket,
        (c.classic_obs + {SHRINK_PSEUDO_BUCKETS} * coalesce(p.classic_prior, 0))
          / (c.classic_exposed_buckets + {SHRINK_PSEUDO_BUCKETS})          AS classic_rate_per_bucket,

        {BUCKETS_PER_HOUR} * (c.ebike_obs + {SHRINK_PSEUDO_BUCKETS} * coalesce(p.ebike_prior, 0))
          / (c.ebike_exposed_buckets + {SHRINK_PSEUDO_BUCKETS})            AS ebike_demand_per_hour,

        -- Latent demand: the uncensored rate applied to ALL time, not just stocked time.
        c.total_buckets * (c.ebike_obs + {SHRINK_PSEUDO_BUCKETS} * coalesce(p.ebike_prior, 0))
          / (c.ebike_exposed_buckets + {SHRINK_PSEUDO_BUCKETS})            AS ebike_latent_total,
        c.ebike_obs                                                        AS ebike_observed_total,

        (c.ebike_exposed_buckets * 1.0 / c.total_buckets < {MIN_AVAILABILITY}
         OR c.ebike_exposed_buckets < {MIN_EXPOSED_BUCKETS})               AS low_confidence
    FROM cells c LEFT JOIN station_prior p USING (station_id)
    """)

    n, st = con.execute("SELECT count(*), count(DISTINCT station_id) FROM demand_baseline").fetchone()
    low = con.execute("SELECT count(*) FROM demand_baseline WHERE low_confidence").fetchone()[0]
    print(f"demand_baseline: {n:,} cells ({st} stations x daytype x hour)")
    print(f"  low confidence: {low:,} ({low/n:.1%}) — thin exposure, do not extrapolate\n")

    tot = con.execute("""SELECT sum(ebike_observed_total), sum(ebike_latent_total)
                         FROM demand_baseline WHERE NOT low_confidence""").fetchone()
    print(f"  ebike departures observed : {tot[0]:>10,.0f}")
    print(f"  ebike demand estimated    : {tot[1]:>10,.0f}")
    print(f"  hidden by stockouts       : {tot[1]-tot[0]:>10,.0f}  (+{(tot[1]/tot[0]-1):.1%})")
    print(f"  ...and this is a LOWER bound; literature puts naive methods ~8% low again\n")

    print("  demand by hour (weekday, ebikes, per station-hour):")
    for h, d, a in con.execute("""
        SELECT hour, avg(ebike_demand_per_hour), avg(ebike_availability)
        FROM demand_baseline WHERE daytype='weekday' AND NOT low_confidence
        GROUP BY 1 ORDER BY 1""").fetchall():
        print(f"    {h:02d}:00  {'█'*int(d*26):<26} {d:.2f}/hr   stocked {a:.0%}")
    con.close()


if __name__ == "__main__":
    sys.exit(main())
