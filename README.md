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

## Quick start

Python 3 only — **no dependencies, no venv**. Stdlib `urllib`, `sqlite3`, `json`, `gzip`.

```bash
python3 poller.py          # one poll; safe to run any time
python3 check_health.py    # collection audit + Phase 1 gate

./scripts/install-poller.sh   # install the 5-min launchd job (idempotent)
launchctl bootout gui/$(id -u)/com.harris.baywheels-poller   # stop it
```

Data lands in `data/baywheels.db` (gitignored — regenerable, and too large for git).

---

## What gets collected

Source: `https://gbfs.lyft.com/gbfs/2.3/bay/gbfs.json` (GBFS **v2.3**, feed ttl 60s).
Feed URLs are resolved from the index on every run rather than hardcoded — Lyft has moved these
paths before, and the index costs 3.5KB.

| Table | Contents | Cadence |
|---|---|---|
| `station_status` | Bikes/ebikes/docks available, disabled counts, renting & returning flags | every 5 min |
| `station_info` | Name, short_name, lat/lon, capacity, region — one row per station **per day** | daily |
| `poll_log` | Every attempt, ok or error, with duration | every 5 min |

Raw `station_status` responses are gzipped to `data/raw/` and kept **48 hours**, so a parsing bug
is recoverable rather than fatal.

**Every poll is stored, including unchanged states.** Change-data-capture would halve the storage
but make gap analysis ambiguous — an absent row could mean "nothing changed" or "we missed it",
and the whole point of Phase 0 is an auditable observation series. ~633 rows per poll,
~182k rows/day, roughly 20 MB/day.

---

## Data gotchas found while building

Worth knowing before writing any analysis against this feed:

1. **`num_bikes_available` includes ebikes.** Classic bikes = `num_bikes_available -
   num_ebikes_available`. This matters for the starvation definition: a station holding only
   ebikes is not starved for an ebike rider but *is* for someone who wanted a classic bike.
   Verified against `vehicle_types_available` (5 classic + 1 ebike summed to 6 available).
2. **`station_id` is a UUID, not the ID used in historical trip CSVs.** `short_name`
   (e.g. `SJ-Q11`) is the likely join key. `station_info` captures it so the Phase 1 join is
   possible — confirm the mapping before trusting any trip-to-station join.
3. **Some stations report stale.** ~25 of 633 had `last_reported` more than an hour behind the
   poll time on day one. A station that stopped reporting is not the same as a station with zero
   bikes, and conflating them will inflate starvation counts.
4. **`is_installed` / `is_renting` / `is_returning` are separate flags.** A station can be
   installed but not renting. Starvation requires `is_renting = 1` — otherwise you are counting
   a decommissioned station as a service failure.

---

## Collection reliability

`check_health.py` enforces the Phase 1 gate: **7 consecutive days at <2% missing polls.** It
reports the gap rate, the longest silence, error detail, and stale-station counts.

The known weakness is the host. launchd does not run jobs while the machine is asleep, so a
laptop that closes overnight will breach a 2% gate (which allows only ~3.4 hours per week). The
poller reads its paths from `BW_DB` / `BW_RAW` and holds no local state, so moving it to an
always-on host is a copy and a cron line. Run the audit daily and escalate if it drifts.

---

## Layout

```
poller.py        one invocation = one poll. Scheduling is launchd's job.
check_health.py  collection audit + Phase 1 gate
schema.sql       three tables, applied idempotently on every run
scripts/         launchd plist + installer
data/            SQLite db + 48h raw archive (gitignored)
```

Design note: a crashed run is a **logged gap, not a dead daemon** — there is no long-lived
process to babysit, and a failure affects one poll rather than the whole collection.
