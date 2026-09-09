"""Postgres storage with change-data-capture, for the Vercel-hosted poller."""

import os
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg

from . import gbfs

ROOT = Path(__file__).resolve().parent.parent
STATUS_FIELDS = gbfs.STATUS_FIELDS


def _load_dotenv():
    """Read .env for local runs so the connection string never has to be typed into a
    shell command (and therefore into shell history). On Vercel the env vars are already
    set and there is no .env, so this is a no-op there."""
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip("'\""))


def _dsn():
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    if not dsn:
        _load_dotenv()
        dsn = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set (and no .env found)")
    return dsn


def connect():
    # autocommit: each poll is a handful of small statements; an open transaction
    # held across a network fetch is exactly what exhausts a pooled connection limit.
    #
    # prepare_threshold=None disables prepared statements. psycopg3 prepares any
    # statement it sees 5 times, and Supabase's transaction-mode pooler (Supavisor on
    # port 6543) does NOT support prepared statements. Without this the poller works
    # for four polls and then starts failing — set it explicitly rather than relying on
    # whichever port the connection string happens to use.
    return psycopg.connect(_dsn(), autocommit=True, prepare_threshold=None)


def ensure_schema(conn):
    conn.execute((ROOT / "schema_pg.sql").read_text())


def _ts(unix):
    return datetime.fromtimestamp(unix, timezone.utc) if unix else None


def _refresh_station_info(conn, feeds, today):
    """Daily. Cheap to check, and catches stations opening or closing mid-collection."""
    hit = conn.execute(
        "SELECT 1 FROM station_info WHERE fetched_on = %s LIMIT 1", (today,)
    ).fetchone()
    if hit:
        return 0
    stations = gbfs.fetch_info(feeds)
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO station_info
               (station_id, fetched_on, name, short_name, lat, lon, region_id, capacity, address)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (station_id, fetched_on) DO NOTHING""",
            [
                (s["station_id"], today, *(s.get(f) for f in gbfs.INFO_FIELDS))
                for s in stations
            ],
        )
    return len(stations)


def poll_once():
    """One poll. Returns a summary dict. Never raises — failures are logged as gaps."""
    started = time.time()
    polled_at = datetime.now(timezone.utc)
    stations_seen = rows_written = 0
    feed_last_updated = None
    status, error, info_added = "ok", None, 0

    conn = connect()
    try:
        ensure_schema(conn)
        feeds = gbfs.discover_feeds()
        info_added = _refresh_station_info(conn, feeds, polled_at.date())

        last_updated, stations = gbfs.fetch_status(feeds)
        feed_last_updated = _ts(last_updated)
        stations_seen = len(stations)

        # Change detection against the last known state of every station.
        current = {
            r[0]: tuple(r[1:])
            for r in conn.execute(
                f"SELECT station_id, {', '.join(STATUS_FIELDS)} FROM station_current"
            )
        }

        changed = [s for s in stations if gbfs.state_tuple(s) != current.get(s["station_id"])]

        if changed:
            with conn.cursor() as cur:
                cur.executemany(
                    f"""INSERT INTO station_status
                        (station_id, observed_at, feed_last_updated, last_reported,
                         {', '.join(STATUS_FIELDS)})
                        VALUES ({', '.join(['%s'] * (4 + len(STATUS_FIELDS)))})
                        ON CONFLICT (station_id, observed_at) DO NOTHING""",
                    [
                        (s["station_id"], polled_at, feed_last_updated,
                         _ts(s.get("last_reported")), *gbfs.state_tuple(s))
                        for s in changed
                    ],
                )
                rows_written = len(changed)

                cur.executemany(
                    f"""INSERT INTO station_current
                        (station_id, observed_at, {', '.join(STATUS_FIELDS)})
                        VALUES ({', '.join(['%s'] * (2 + len(STATUS_FIELDS)))})
                        ON CONFLICT (station_id) DO UPDATE SET
                          observed_at = EXCLUDED.observed_at,
                          {', '.join(f'{f} = EXCLUDED.{f}' for f in STATUS_FIELDS)}""",
                    [
                        (s["station_id"], polled_at, *gbfs.state_tuple(s))
                        for s in changed
                    ],
                )
    except Exception as exc:
        status = "error"
        error = f"{type(exc).__name__}: {exc}"[:500]
    finally:
        try:
            conn.execute(
                """INSERT INTO poll_log
                   (polled_at, status, feed_last_updated, stations_seen,
                    rows_written, duration_ms, error)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (polled_at) DO NOTHING""",
                (polled_at, status, feed_last_updated, stations_seen,
                 rows_written, int((time.time() - started) * 1000), error),
            )
        finally:
            conn.close()

    return {
        "status": status,
        "polled_at": polled_at.isoformat(),
        "stations_seen": stations_seen,
        "rows_written": rows_written,
        "unchanged": stations_seen - rows_written,
        "station_info_refreshed": info_added,
        "duration_ms": int((time.time() - started) * 1000),
        "error": error,
    }


# Movement thresholds for vehicle CDC. GPS jitters by a few metres while a bike sits
# still, and range readings wobble; without these floors a parked vehicle would emit a
# row every poll and the table would be mostly noise.
MOVE_M = 25
RANGE_M = 150


def _haversine_m(lat1, lon1, lat2, lon2):
    import math
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def poll_vehicles():
    """One poll of free_bike_status. Independent of the station poller.

    CONSTRAINT FOR ANY ANALYSIS BUILT ON THIS. The "range went up, so the operator
    touched it" inference is only safe while a vehicle is CONTINUOUSLY present in this
    feed. Lyft's Pillar docks recharge an ebike in place over several hours, and
    deployment is partial and market-specific — confirmed for Citi Bike in NYC and
    still expanding there, unconfirmed for Bay Wheels.

    Since free_bike_status contains only vehicles NOT in docks, a bike that vanishes
    from the feed and returns with more range was docked in between, and a charging
    dock may have done the work rather than a van. Those gapped observations must be
    excluded. A range rise across an unbroken run of observations has no dock
    explanation and remains operator-only evidence.
    """
    started = time.time()
    polled_at = datetime.now(timezone.utc)
    seen = written = 0
    status, error = "ok", None

    conn = connect()
    try:
        ensure_schema(conn)
        feeds = gbfs.discover_feeds()
        _, bikes = gbfs.fetch_vehicles(feeds)
        seen = len(bikes)

        current = {
            r[0]: (r[1], r[2], r[3], r[4], r[5])
            for r in conn.execute(
                "SELECT bike_id, lat, lon, range_m, is_disabled, is_reserved FROM vehicle_current")
        }
        changed = []
        for b in bikes:
            bid = b.get("bike_id")
            lat, lon = b.get("lat"), b.get("lon")
            if not bid or lat is None or lon is None:
                continue
            rng = int(b.get("current_range_meters") or 0)
            dis, res = int(b.get("is_disabled") or 0), int(b.get("is_reserved") or 0)
            prev = current.get(bid)
            if prev is not None:
                moved = _haversine_m(prev[0], prev[1], lat, lon) if prev[0] is not None else 1e9
                if (moved < MOVE_M and abs((prev[2] or 0) - rng) < RANGE_M
                        and prev[3] == dis and prev[4] == res):
                    continue
            changed.append((bid, polled_at, lat, lon, rng, dis, res))

        if changed:
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO vehicle_status
                       (bike_id, observed_at, lat, lon, range_m, is_disabled, is_reserved)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (bike_id, observed_at) DO NOTHING""", changed)
                cur.executemany(
                    """INSERT INTO vehicle_current
                       (bike_id, observed_at, lat, lon, range_m, is_disabled, is_reserved)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (bike_id) DO UPDATE SET
                         observed_at=EXCLUDED.observed_at, lat=EXCLUDED.lat, lon=EXCLUDED.lon,
                         range_m=EXCLUDED.range_m, is_disabled=EXCLUDED.is_disabled,
                         is_reserved=EXCLUDED.is_reserved""", changed)
            written = len(changed)
    except Exception as exc:
        status = "error"
        error = f"{type(exc).__name__}: {exc}"[:500]
    finally:
        conn.close()

    return {"status": status, "polled_at": polled_at.isoformat(), "vehicles_seen": seen,
            "rows_written": written, "unchanged": seen - written,
            "duration_ms": int((time.time() - started) * 1000), "error": error}
