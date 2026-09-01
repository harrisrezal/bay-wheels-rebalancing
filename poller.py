#!/usr/bin/env python3
"""Bay Wheels GBFS poller — Phase 0.

GBFS publishes only the current snapshot; there is no history endpoint. The dataset this
project needs does not exist until this runs, so this is the first thing built and the last
thing stopped.

One invocation = one poll. Scheduling is launchd's job, not ours: a crashed run is a logged
gap, not a dead daemon. Stdlib only, so it runs under any python3 with no venv.
"""

import gzip
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

GBFS_INDEX = os.environ.get(
    "GBFS_INDEX", "https://gbfs.lyft.com/gbfs/2.3/bay/gbfs.json"
)
ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("BW_DB", ROOT / "data" / "baywheels.db"))
RAW_DIR = Path(os.environ.get("BW_RAW", ROOT / "data" / "raw"))
RAW_RETENTION_HOURS = 48
HTTP_TIMEOUT = 20
USER_AGENT = "bay-wheels-rebalancing/0.1 (personal research project)"

STATUS_FIELDS = (
    "num_bikes_available",
    "num_ebikes_available",
    "num_bikes_disabled",
    "num_docks_available",
    "num_docks_disabled",
    "is_installed",
    "is_renting",
    "is_returning",
)


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        raw = resp.read()
    return json.loads(raw), raw


def fetch_json_retry(url, attempts=2):
    """One retry. A second failure is a real gap and gets logged as one."""
    last = None
    for i in range(attempts):
        try:
            return fetch_json(url)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last = exc
            if i + 1 < attempts:
                time.sleep(2)
    raise last


def discover_feeds(index_url):
    """Resolve feed URLs from the index every run rather than hardcoding deep paths —
    Lyft has moved these before, and the index is 3.5KB."""
    doc, _ = fetch_json_retry(index_url)
    feeds = doc["data"]["en"]["feeds"]
    return {f["name"]: f["url"] for f in feeds}, doc.get("version")


def connect(db_path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def ensure_schema(conn):
    conn.executescript((ROOT / "schema.sql").read_text())
    conn.commit()


def archive_raw(raw_bytes, polled_at):
    """Keep 48h of raw responses so a parsing bug is recoverable (PRD F0.6)."""
    stamp = datetime.fromtimestamp(polled_at, timezone.utc)
    day_dir = RAW_DIR / stamp.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    out = day_dir / f"station_status_{stamp.strftime('%H%M%S')}.json.gz"
    with gzip.open(out, "wb") as fh:
        fh.write(raw_bytes)


def prune_raw(now):
    cutoff = datetime.fromtimestamp(now, timezone.utc) - timedelta(hours=RAW_RETENTION_HOURS)
    if not RAW_DIR.exists():
        return
    for day_dir in RAW_DIR.iterdir():
        if not day_dir.is_dir():
            continue
        try:
            day = datetime.strptime(day_dir.name, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if day < cutoff - timedelta(days=1):
            for f in day_dir.iterdir():
                f.unlink()
            day_dir.rmdir()


def refresh_station_info(conn, info_url, today):
    """Daily refresh. Cheap to check, and catches stations opening/closing mid-collection."""
    row = conn.execute(
        "SELECT 1 FROM station_info WHERE fetched_on = ? LIMIT 1", (today,)
    ).fetchone()
    if row:
        return 0
    doc, _ = fetch_json_retry(info_url)
    stations = doc["data"]["stations"]
    conn.executemany(
        """INSERT OR REPLACE INTO station_info
           (station_id, fetched_on, name, short_name, lat, lon, region_id, capacity, address)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        [
            (
                s["station_id"], today, s.get("name"), s.get("short_name"),
                s.get("lat"), s.get("lon"), s.get("region_id"),
                s.get("capacity"), s.get("address"),
            )
            for s in stations
        ],
    )
    conn.commit()
    return len(stations)


def poll():
    started = time.time()
    polled_at = int(started)
    conn = connect(DB_PATH)
    ensure_schema(conn)

    stations_seen = 0
    rows_written = 0
    feed_last_updated = None
    status = "ok"
    error = None

    try:
        feeds, _version = discover_feeds(GBFS_INDEX)
        info_added = refresh_station_info(
            conn, feeds["station_information"],
            datetime.now().astimezone().strftime("%Y-%m-%d"),
        )

        doc, raw = fetch_json_retry(feeds["station_status"])
        feed_last_updated = doc.get("last_updated")
        stations = doc["data"]["stations"]
        stations_seen = len(stations)

        rows = [
            (
                s["station_id"], polled_at, feed_last_updated, s.get("last_reported"),
                *(s.get(f) for f in STATUS_FIELDS),
            )
            for s in stations
        ]
        cur = conn.executemany(
            f"""INSERT OR IGNORE INTO station_status
                (station_id, observed_at, feed_last_updated, last_reported,
                 {", ".join(STATUS_FIELDS)})
                VALUES ({",".join("?" * (4 + len(STATUS_FIELDS)))})""",
            rows,
        )
        rows_written = cur.rowcount
        conn.commit()

        archive_raw(raw, polled_at)
        prune_raw(polled_at)
        msg = f"ok stations={stations_seen} rows={rows_written}"
        if info_added:
            msg += f" station_info_refreshed={info_added}"
    except Exception as exc:  # logged as a gap, never swallowed
        status = "error"
        error = f"{type(exc).__name__}: {exc}"[:500]
        msg = f"ERROR {error}"

    conn.execute(
        """INSERT OR REPLACE INTO poll_log
           (polled_at, status, feed_last_updated, stations_seen, rows_written, duration_ms, error)
           VALUES (?,?,?,?,?,?,?)""",
        (
            polled_at, status, feed_last_updated, stations_seen, rows_written,
            int((time.time() - started) * 1000), error,
        ),
    )
    conn.commit()
    conn.close()

    print(f"[{datetime.fromtimestamp(polled_at).isoformat(timespec='seconds')}] {msg}")
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    sys.exit(poll())
