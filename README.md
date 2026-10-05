# Box Break Finder

"The Google Flights of box breaks" — aggregates sports-card box breaks from
eBay and YouTube into one searchable site. Pure aggregator: no breaks happen
here, no checkout, no streaming. See `../your_files/box-break-aggregator-v1-map.md`
for the full product plan.

## What works (Phase 1)
- **eBay ingestion** (`app/ebay.py` + `app/ingest.py`): OAuth client-credentials
  flow + Browse API `item_summary/search`, normalized into Postgres.
- **YouTube ingestion** (`app/youtube.py` + `app/ingest.py --source youtube`):
  Data API v3 `search.list` (live + upcoming) + `videos.list` details,
  normalized into Postgres. Phase 2 complete — see "YouTube quota" below.
- **Normalizer** (`app/normalizer.py`, `app/products.py`): sport/format
  detection, price parsing, ~20-product alias table.
- **Break grouping** (`app/grouping.py`): groups eBay slot listings into
  likely breaks by seller + break number + product.
- **Twitch ingestion** (`app/twitch.py` + `app/ingest.py --source twitch`):
  Helix API OAuth client-credentials + `search/channels` (live only) +
  `streams` enrichment, normalized into Postgres. See "Twitch rate limit"
  below.
- **Web UI** (`app/main.py` + `templates/`): server-rendered search with
  filters (sport, product text, format, max price, live now, platform) and a
  detail page with a prominent outbound link.

## Setup (no coding experience needed beyond copy-paste)

1. **Python 3.11+** and **Postgres** installed.

2. Create the database and load the schema:
   ```bash
   createdb breaks
   psql "$DATABASE_URL" -f schema.sql
   ```

3. Install dependencies:
   ```bash
   cd box-break-app
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```

4. Configure credentials (never commit real values):
   ```bash
   cp .env.example .env
   # edit .env: set EBAY_APP_ID and EBAY_CERT_ID from developer.ebay.com
   ```
   The app reads `.env` automatically via `python-dotenv`.

5. (Optional) Load demo rows to see the UI without API keys:
   ```bash
   python -m app.ingest --demo
   ```

6. Run the web app:
   ```bash
   uvicorn app.main:app --reload
   # open http://localhost:8000
   ```

7. Pull real eBay breaks (needs the keys from step 4):
   ```bash
   python -m app.ingest
   ```
   Run this on a schedule (cron every ~15 min) to keep listings fresh.

8. Pull YouTube break streams (needs `YOUTUBE_API_KEY` in `.env`):
   ```bash
   python -m app.ingest --source youtube
   ```
   Run this on a schedule (cron every ~6 hours — see "YouTube quota" below).

9. Pull Twitch live break streams (needs `TWITCH_CLIENT_ID` and
   `TWITCH_CLIENT_SECRET` in `.env`, from dev.twitch.tv/console):
   ```bash
   python -m app.ingest --source twitch
   ```
   Run this on a schedule (cron every ~15-30 min — see "Twitch rate limit"
   below).

## Twitch rate limit
Helix app tokens allow **800 requests/min** — the API limit is not the
constraint. A run makes ~5 requests (4 channel searches + 1 batched streams
enrichment), so even a 15-minute cadence is ~480 requests/day, far under the
limit. The cadence choice is about freshness: live streams turn over fast,
so **every 15–30 min** keeps the "LIVE" view accurate. The query list is
configurable without code changes:
`TWITCH_SEARCH_QUERIES="box break,card break,live breaks"` (comma-separated).

## YouTube quota
YouTube Data API v3 free quota is **10,000 units/day**:
- `search.list` = **100 units/call** (cost is per call, not per result — we
  always request `maxResults=50` for full value)
- `videos.list` = **1 unit/call** (up to 50 video ids per call)

Default run: 3 queries × 2 event types (live, upcoming) = 6 searches
(600 units) + ~3 detail calls ≈ **603 units/run**.

Recommended cadence: **every 6 hours (4×/day) ≈ 2,400 units/day (~24%)**,
leaving headroom for more queries or runs. Do NOT poll YouTube every
15 minutes like eBay — hourly runs would burn ~14,500 units/day and blow
the quota. The query list is configurable without code changes:
`YOUTUBE_SEARCH_QUERIES="box break live,card break"` (comma-separated).

## Project layout
```
box-break-app/
  app/
    __init__.py
    config.py       # env-only config; secrets never logged
    db.py           # psycopg helpers: upsert + search queries
    ebay.py         # OAuth client-credentials + Browse API search
    twitch.py       # Helix API poller (OAuth client-credentials, rate-light)
    normalizer.py   # sport/format detection, price parsing, product match
    products.py     # starter product alias table (~20 products)
    grouping.py     # group eBay slot listings into breaks
    youtube.py      # YouTube Data API v3 poller (quota-budgeted)
    ingest.py       # CLI runner: python -m app.ingest [--source ebay|youtube|twitch] [--demo]
    main.py         # FastAPI + Jinja2 UI
  templates/
    search.html
    detail.html
  schema.sql
  requirements.txt
  .env.example
```

## Notes
- Secrets live only in environment variables (`EBAY_APP_ID`, `EBAY_CERT_ID`,
  `YOUTUBE_API_KEY`, `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`). They are
  never hardcoded and never logged.
- No live API calls happen unless you run `python -m app.ingest` with keys set.

## Deploying to Render (production)

`Dockerfile` + `render.yaml` are ready. One-time setup:

1. Push this folder to a GitHub repo.
2. In Render: New + -> Blueprint, point at the repo (`render.yaml`).
   This creates: web service `break-alley`, Postgres `break-alley-db`,
   and 3 cron pollers (eBay every 15 min, YouTube every 6 h, Twitch every 20 min).
3. Fill in secret env vars in the Render dashboard (all marked `sync: false`):
   `EBAY_APP_ID`, `EBAY_CERT_ID`, `EBAY_CAMPID` (optional),
   `YOUTUBE_API_KEY`, `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`.
   Values live in the local `.env` (never committed).
4. The web service runs `schema.sql` on boot (idempotent), then serves.

Approx cost: web $7 + Postgres $6 + 3 cron jobs $1 each = ~$16/mo.
