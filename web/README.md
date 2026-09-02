# Fleet Monitor (web)

Next.js + deck.gl visualisation of the Bay Wheels collection data.

## Run locally

```bash
cd web
npm install
npm run dev          # http://localhost:3000
```

## Refresh the data

The page reads a single static file, `public/fleet.json`. Regenerate it from the
local DuckDB store after syncing collection:

```bash
cd ..
.venv/bin/python analysis/sync_from_postgres.py
.venv/bin/python analysis/build_grid.py
.venv/bin/python analysis/build_deltas.py
.venv/bin/python analysis/export_web.py     # writes web/public/fleet.json
```

Nothing in the browser touches Postgres: no API route, no connection pool, no
Supabase egress, and the CDN gzips the file. Redeploy to publish new data.

## Deploy

This repo holds two Vercel projects:

| Project | Root directory | What it is |
|---|---|---|
| collection | `/` (repo root) | Python cron poller, `vercel.json` holds the schedule |
| web | `web/` | This app |

Create the second project with **Root Directory = `web`**, otherwise Vercel will
try to build the poller and this app together.

## Notes

- Basemap is CARTO's free dark style; no API key, no account.
- Van events render as pulses at the station where they happened. Pickups at one
  station and dropoffs at another are never linked into routes — the data shows
  that both occurred, never that the same vehicle did both, and drawing arcs
  between them would be inventing evidence.
- Single-theme by intent: the CARTO dark basemap cannot follow the viewer's colour
  scheme, so the page commits to the night operations look and states every colour.
