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
