"""GBFS feed access, shared by the local SQLite poller and the Vercel function.

Stdlib only so the local poller keeps working without installing anything.
"""

import json
import time
import urllib.error
import urllib.request

GBFS_INDEX = "https://gbfs.lyft.com/gbfs/2.3/bay/gbfs.json"
HTTP_TIMEOUT = 15
USER_AGENT = "bay-wheels-rebalancing/0.2 (personal research project)"

# The inventory fields that define a station's state. Deliberately EXCLUDES
# last_reported and feed_last_updated: those tick on nearly every poll, so
# including them in change detection would make change-data-capture useless
# (every station would look "changed" every time).
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

INFO_FIELDS = ("name", "short_name", "lat", "lon", "region_id", "capacity", "address")


def fetch_json(url, attempts=2):
    """One retry. A second failure is a real gap and gets logged as one."""
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last = exc
            if i + 1 < attempts:
                time.sleep(1)
    raise last


def discover_feeds(index_url=GBFS_INDEX):
    """Resolve feed URLs from the index rather than hardcoding deep paths —
    Lyft has moved these before, and the index is 3.5KB."""
    doc = fetch_json(index_url)
    return {f["name"]: f["url"] for f in doc["data"]["en"]["feeds"]}


def fetch_status(feeds):
    doc = fetch_json(feeds["station_status"])
    return doc.get("last_updated"), doc["data"]["stations"]


def fetch_info(feeds):
    doc = fetch_json(feeds["station_information"])
    return doc["data"]["stations"]


def fetch_vehicles(feeds):
    """Free-floating vehicles with individual ids and battery range."""
    doc = fetch_json(feeds["free_bike_status"])
    return doc.get("last_updated"), doc["data"]["bikes"]


def state_tuple(station):
    """The comparable state of a station, for change detection."""
    return tuple(station.get(f) for f in STATUS_FIELDS)
