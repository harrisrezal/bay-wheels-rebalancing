"""Vercel entrypoint for Bay Wheels collection.

Vercel's cron makes an HTTP GET to /api/poll. Delivery is best effort with no retry
and occasional duplicate invocations, so this handler is idempotent by design:
station_status has a (station_id, observed_at) primary key with ON CONFLICT DO NOTHING,
and a duplicate poll writes nothing new.
"""

import hmac
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from lib import store_pg

app = FastAPI(docs_url=None, redoc_url=None)


def _authorized(request: Request) -> bool:
    """Vercel sends `Authorization: Bearer $CRON_SECRET` on cron invocations.

    Without this check the endpoint is a public URL anyone can hammer — it writes to
    the database and calls out to Lyft's feed on every hit. Fail CLOSED if the secret
    is unset, so a misconfigured deploy is inert rather than open.
    """
    secret = os.environ.get("CRON_SECRET")
    if not secret:
        return False
    return hmac.compare_digest(
        request.headers.get("authorization", ""), f"Bearer {secret}"
    )


@app.get("/api/poll")
def poll(request: Request):
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    result = store_pg.poll_once()
    # 200 even on a logged collection error: the invocation itself succeeded and the
    # gap is recorded in poll_log. Returning 5xx would only add noise to Vercel's logs
    # without changing behaviour, since cron does not retry.
    return JSONResponse(result)


@app.get("/api/poll-vehicles")
def poll_vehicles(request: Request):
    """Free-floating vehicles, on a slower cadence than stations.

    Runs every 30 minutes. The signal being chased — a battery charged or swapped, a
    vehicle carried rather than ridden — changes on the order of hours, so fine
    resolution buys nothing. At 10 minutes this cost 9-25 MB/day against the station
    feed's 8.2, which would have cut free-tier runway from seven weeks to about three.

    This is a TIME-BOXED EXPERIMENT, not permanent collection: enough days to test
    whether operator intervention on individual vehicles corroborates the van events
    inferred from station counts, then switch it off.
    """
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse(store_pg.poll_vehicles())


@app.get("/api/health")
def health(request: Request):
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    conn = store_pg.connect()
    try:
        row = conn.execute(
            """SELECT count(*) FILTER (WHERE status = 'ok'),
                      count(*) FILTER (WHERE status <> 'ok'),
                      min(polled_at), max(polled_at)
               FROM poll_log"""
        ).fetchone()
        rows, stations = conn.execute(
            "SELECT count(*), count(DISTINCT station_id) FROM station_status"
        ).fetchone()
    finally:
        conn.close()

    ok, errors, first, last = row
    return JSONResponse({
        "polls_ok": ok,
        "polls_error": errors,
        "first_poll": first.isoformat() if first else None,
        "last_poll": last.isoformat() if last else None,
        "status_rows": rows,
        "stations": stations,
    })
