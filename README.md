# Bay Wheels Rebalancing

Can you predict where a bikeshare fleet will fail — and move bikes in time to prevent it?

Docked bikeshare fails in two directions. A station with **zero bikes** turns away a rider who
wanted one. A station with **zero free docks** turns away a rider trying to end a trip. Neither
failure appears in trip data, because the failure *is* the trip that never got recorded. Operators
run rebalancing trucks to move bikes between stations, but truck capacity and drive time are
finite — so every rebalancing decision is a bet on where demand will be in 30–90 minutes.

That is structurally the same problem as robotaxi repositioning: forecast demand, move idle supply
ahead of it, pay a deadhead cost to do so.

This project studies it on **Bay Wheels** (San Francisco, Lyft-operated, 633 stations), using
public data.

---

## Findings

*Phase 1 in progress — collection started 2026-09-01. Findings land here, above the fold.*

One number from the first poll, as a preview of what Phase 1 will quantify properly:
**83 of 633 stations (13%) had zero bikes available while still marked as renting.**
Whether that constitutes *failure* depends on whether anyone wanted a bike there — which is
exactly what Phase 1 has to establish before the number means anything.

---

## Status

| Phase | What it answers | State |
|---|---|---|
| **0 — Collection** | — | ✅ **Running** |
| 1 — Observability | Where and when does the fleet actually fail? | Blocked on 7 days of data |
| 2 — Forecast | Can you see failure coming early enough to act? | Not started |
| 3 — Recommend | Can you prevent it at acceptable cost? | Not started |

---

## Why Phase 0 comes first

GBFS — the **General Bikeshare Feed Specification**, the open standard operators publish under
city permit conditions — serves only the *current snapshot*. There is no history endpoint and no
stream. Ask it for last Tuesday and there is no answer.

**So the dataset does not exist until the poller runs.** Every day of delay is a day of data that
can never be recovered, and Phase 1 is gated on seven consecutive days. That is why collection
was built and started before anything else was designed.

---

## Deployment

Collection runs as a **Vercel cron job** writing to **Postgres**. It previously ran on a
laptop via launchd; four hours of real data showed 54% bucket coverage against a 98% gate,
almost entirely from machine sleep — the feed itself was ~100% reliable over the same window.

```
Vercel cron (*/2)  ──GET /api/poll──→  app.py (FastAPI)
                                          │
                     Lyft GBFS ───────────┤
                                          ↓
                                      Postgres  (station_status, station_current,
                                                 station_info, poll_log)
```

Polling every **2 minutes** for a **5-minute** analysis grid is deliberate. Vercel documents
cron delivery as *best effort with no retry*, so over-scheduling means a dropped invocation
costs redundancy rather than coverage.

### Setup

1. Provision Postgres (Neon via the Vercel Marketplace, Supabase, or any Postgres) and set
   `DATABASE_URL` in the Vercel project
2. Set `CRON_SECRET` to a random string of 16+ characters — **required**, see Security below
3. Deploy. `vercel.json` registers the cron; `schema_pg.sql` applies itself on first poll
4. `python3 backfill_sqlite_to_pg.py --dry-run` to preview, then without the flag to migrate
   locally-collected rows

### Local use

```bash
python3 poller.py                       # local SQLite poller, stdlib only, still works
python3 check_health.py                 # reads DATABASE_URL if set, else local SQLite
python3 backfill_sqlite_to_pg.py --dry-run
```

## Security

`/api/poll` and `/api/health` are public URLs. Both verify the
`Authorization: Bearer $CRON_SECRET` header that Vercel attaches to cron invocations,
compared with `hmac.compare_digest`. **The check fails closed** — if `CRON_SECRET` is unset
the endpoints return 401 rather than running, so a misconfigured deploy is inert instead of
an open write endpoint pointed at someone else's API.

## What gets collected

Source: `https://gbfs.lyft.com/gbfs/2.3/bay/gbfs.json` (GBFS **v2.3**, 633 stations, ttl 60s).
Feed URLs are resolved from the index every run rather than hardcoded — Lyft has moved these
paths before, and the index costs 3.5KB.

| Table | Contents | Cadence |
|---|---|---|
| `station_status` | **Changed** inventory states only | every 2 min |
| `station_current` | Latest state per station (drives change detection) | every 2 min |
| `station_info` | Name, short_name, lat/lon, capacity — one row per station per day | daily |
| `poll_log` | Every attempt, ok or error | every 2 min |

### Change-data-capture

Only rows where a station's inventory **changed** are stored. Measured against 4 hours of
full-fidelity local collection: **21.1% of rows kept, 78.9% dropped** — 247 MB/week becomes
~52 MB/week.

This is unambiguous because `poll_log` records every successful poll: an absent row plus a
logged poll means "no change", never "we missed it". Reconstruct the full series by
forward-filling between rows, bounded by poll_log coverage.

Change detection deliberately **excludes `last_reported` and `feed_last_updated`** — those
tick on nearly every poll, so including them would mark every station changed every time and
defeat CDC entirely.

## Collection reliability

The gate is **bucket coverage**, not poll count: the share of 5-minute buckets containing at
least one observation. Poll count is only a proxy for that, and an inexact one once the
scheduler is best-effort rather than deterministic. Phase 1 requires **7 consecutive days at
≥98% coverage**.

Vercel also documents that cron may invoke the same run more than once. Writes are idempotent
— `station_status` has a `(station_id, observed_at)` primary key with `ON CONFLICT DO NOTHING`
— so a duplicate poll writes nothing.

## Layout

```
app.py                 Vercel entrypoint: /api/poll (cron), /api/health
vercel.json            cron schedule + function config
lib/gbfs.py            feed discovery + fetch, shared by both entrypoints
lib/store_pg.py        Postgres writes with change-data-capture
schema_pg.sql          Postgres schema
backfill_sqlite_to_pg.py   migrate local SQLite rows into Postgres (CDC-converted)
poller.py              local SQLite poller, stdlib only — fallback and local dev
schema.sql             local SQLite schema
check_health.py        collection audit + Phase 1 gate (Postgres or SQLite)
scripts/               launchd plist + installer (local fallback)
```

Design note: a crashed run is a **logged gap, not a dead daemon** — there is no long-lived
process to babysit, and a failure affects one poll rather than the whole collection.
