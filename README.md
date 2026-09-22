# PlayMatch (v1)

Price match and price history for PC games across Steam, GOG and Epic Games. US region first; other regions are a config change.

## Layout
- `price-match/` FastAPI service: connectors, matcher, FX, catalog sync, bulk Steam prices, GOGDB importer, daily job, price + history + stats API, `/metrics`
- `frontend/` static UI served by nginx: `index.html` (page shell + sticky nav), `app.css`, `app.js`, `fonts/`. No build step; see "Frontend" below
- `docker-compose.yml` db (Postgres), price-match, scheduler (daily job), frontend, and a one-shot `snapshot` job

## Run with Docker
    cp .env.example .env          # set POSTGRES_PASSWORD (and ADMIN_TOKEN if you want admin endpoints)
    docker compose up -d --build
    docker compose run --rm snapshot --seed     # seed 10 games, fetch Steam/Epic, import GOGDB dump
    open http://localhost:8080

The `scheduler` service then runs the same job daily at `SCHEDULE_HOUR_UTC` (default 06:00 UTC).

## Frontend
Hash-routed single page: `#/` is the games list (search + cover-art grid) and `#/game/<id>` is a game profile (cover art, prices by store, price-history chart). The nav bar is sticky; the currency and region selectors live in it, and new sections are added as links in `index.html`. The "All games" link, the brand and the Games link all return to the list with the search text and scroll position kept.

- **Cover art:** `GET /api/games` and `/api/games/{id}/prices` return `image_url` (Steam's `header.jpg` for games with a `steam_app_id`, else `null`). The UI draws a text tile when it is `null` or the image fails to load. To use other art, change `game_dict` in `app/service.py`.
- **History chart:** every sample is a marker, drawn as a step line (a price holds until the next sample). Date labels sit on real sample dates when they fit, otherwise on evenly spaced dates. Hover or use the arrow keys to read every store's price on a sample date. "All sample dates" lists them as a table.
- **Fonts** (Bricolage Grotesque, Instrument Sans) are self-hosted in `frontend/fonts/`, so the page makes no third-party font requests. Cover art is the one external request (Steam's CDN).

## API notes
- `GET /api/games?q=&limit=&offset=`: `limit` 1 to 100 (default 20), `offset` from 0. The body is a list; the number of games matching `q` is in the `X-Total-Count` header.
- `GET /api/games/{id}/prices` also returns `checks` (`steam`, `gog`, `epic`: when each store was last checked, or `null`) and a `checked_at` on every store row. Snapshots are stored only when a price changes, so `checked_at` can be newer than the snapshot it belongs to.
- `POST /api/games/{id}/epic-check?region=US` looks up the Epic price if the game was not checked in the last `EPIC_COOLDOWN_HOURS`. It returns `{"status": ..., "checked_at": ...}` with status `fresh` (no request made), `checked`, `busy` (over `EPIC_ON_DEMAND_PER_MINUTE` in this process) or `unavailable` (Epic rate limited us, so lookups pause for 15 minutes, or the lookup failed). A game Epic does not list still counts as checked, so it is not searched again within the cooldown. The limiter is per process: with several API workers each has its own limit.

### Browser checks
    cd price-match
    pip install playwright && python -m playwright install chromium
    DATABASE_URL=sqlite:///demo.db python -m scripts.seed_demo
    DATABASE_URL=sqlite:///demo.db FRONTEND_DIR=../frontend python -m uvicorn app.main:app --port 8765
    python -m scripts.browser_check http://localhost:8765/      # in another terminal

The checks intercept every Epic lookup, so they never contact Epic. Delete `demo.db` afterwards.

## Data sources
| Store | How | Notes |
| --- | --- | --- |
| Steam | Catalog: `IStoreService/GetAppList` (needs `STEAM_API_KEY`). Prices: batched `appdetails` calls, several games per request | Unofficial price endpoint. `REQUEST_DELAY` spacing applies per request |
| Epic | Storefront GraphQL, live | Unofficial. Same politeness settings |
| GOG | GOGDB daily dump (gogdb.org/backups_v3), one ~60 MB download per day | Also backfills real price history. No requests to GOG itself |
| G2A | Dropped: no open API and scraping would breach terms | |

Rate-limit safety: requests are spaced by `REQUEST_DELAY`; on the first HTTP 429/403 from a store the job stops calling that store for the rest of the run and records `rate_limited`. Never raise concurrency; lower it if anything.

### GOGDB layout caveat
The dump's internal layout could not be checked when this was written. The importer searches for `<id>/product.json` and `<id>/prices.json` and accepts several price shapes. If the log shows `no prices parsed`, run:

    docker compose run --rm --entrypoint python snapshot -m app.gogdb --inspect /cache/gogdb_YYYY-MM-DD.tar.xz

and adjust `app/gogdb.py` (`extract_prices`). Also check that prices come out right (cents vs dollars).
Bulk Steam prices are stored when they change plus a weekly heartbeat, Epic seed games are stored daily, and GOG rows come at its price changes, so per-store sample counts and averages are not directly comparable.

## Full Steam catalog
The daily job (`python -m app.snapshot`) runs, in order: catalog sync (lists every Steam base game), FX refresh, bulk Steam prices, Epic prices for the seed games only, and the GOGDB import.

- **Catalog:** needs a free Steam Web API key (https://steamcommunity.com/dev/apikey) in `.env` as `STEAM_API_KEY`. Without it the step is skipped and the existing games keep working. Two Steam apps with the same title stay two games. The key is redacted from the HTTP request log lines (`key=REDACTED`), so it does not leak into job or Docker logs.
- **Steam prices:** `STEAM_BATCH_SIZE` games per request (default 50). A batch that fails is split in half and retried. A single game that keeps failing is skipped and retried next run. After 10 failed Steam requests in a row (a real outage) the run stops early, logs one error, and leaves the remaining games unchecked so they go first next run. A game is marked checked (table `price_checks`) even when Steam returns no price (free to play, unreleased, delisted). Never-checked games are fetched first, so a run cut short resumes where it stopped. Steam throttles the price endpoint, and when it does the run stops and resumes where it stopped on the next run, so the first full pass over a very large catalog may take several daily runs; on the first real-key run, note how many batches succeeded before any rate limit (the run report shows `batches` and `rate_limited`).
- **Snapshots:** bulk Steam snapshots are stored only when a price changes, plus one every `SNAPSHOT_HEARTBEAT_DAYS` (default 7); the chart already treats a price as holding until the next sample. Epic prices for the seed games are stored on every daily run, and GOG rows come from GOGDB's own price-change records.
- **GOG for catalog games:** catalog games have no release year, so only exact title matches (after normalizing) link automatically. Near-matches go to the manual review queue, at most `REVIEW_QUEUE_CAP_PER_RUN` (default 200) per run, highest score first. Only candidates not already in the review queue count toward the cap, so old entries never use it up.
- **Epic:** only seed games (`app/seed_games.json`) are refreshed daily. Other games get Epic prices only when an admin refreshes them (`POST /api/admin/games/{id}/refresh`).

### First run with a key
    docker compose up -d --build
    docker compose run --rm snapshot --seed --skip-gogdb     # lists every game, prices them all
    docker compose run --rm --entrypoint python snapshot -m app.bulk --probe

The first command prints the catalog sync result and the bulk report. In the sync result, `added` counts games new to the database (games already there, such as the 10 seed games loaded first, are not counted) and `total` is the size of Steam's list, so `total` is the number to look at for how many games Steam has. If `catalog.sync` printed `{'skipped': 'error: ...'}` the API key was rejected: check the log line. A response shape the parser does not understand usually shows up as `{'added': 0, ..., 'total': 0}` instead of a skip. The probe tests two batch sizes, 50 and 100, and prints the larger one that Steam fully answers (100 means at least 100 works, not a proven maximum); set `STEAM_BATCH_SIZE` to it in `.env`.

### Scale results
Measured on a 100,000-game Postgres 16 database (see `price-match/scripts/scale_test.py`). The games are synthetic: random 2-4 word titles from a 40-word list, and the GOG pool is 12,000 synthetic titles. The matching figure is measured on a sample of 3,000 games and extrapolated linearly to 100,000. Real titles are longer, so it is probably a lower bound, and it covers only the fuzzy-matching call, not `evaluate()` or database work.

| Check | Result |
| --- | --- |
| Substring search on Postgres, 4 queries, 3 runs (limit 48, ordered) | 'dragon' 72.0 / 69.2 / 78.2 ms; 'dark soul' 10.3 / 11.3 / 13.7 ms; 'zzzz' 9.5 / 10.8 / 12.1 ms; 'a' 1.7 / 1.6 / 1.8 ms. All under the 200 ms limit |
| GOG fuzzy matching, projected for 100,000 games | Extract loop: 0.89 ms/game, 89 s projected (limit 600 s). Chunked cdist (numpy, all cores): 0.03 ms/game, 3 s projected, not adopted |
| Index / matching change applied | None. Neither decision rule fired: no `pg_trgm` index, no chunked `cdist` |

To repeat the benchmark, run from `price-match/`:

    DATABASE_URL=postgresql+psycopg://pm:pm@localhost:5433/pm python -m scripts.scale_test populate --games 100000
    DATABASE_URL=postgresql+psycopg://pm:pm@localhost:5433/pm python -m scripts.scale_test search
    python -m scripts.scale_test match --games 100000 --pool 12000 --sample 3000

`populate` refuses to run unless `DATABASE_URL` is set. It writes 100,000 synthetic games to that database, so point it at a throwaway one. `match` does not use a database.

## Regions
`REGIONS=US` by default. `REGIONS=US,GB,DE` enables more (see `app/regions.py`): connectors query that storefront, listings and snapshots are stored per region, the API takes `?region=`, and the UI shows a region selector once more than one is enabled. Costs scale linearly: each extra region multiplies live Steam/Epic requests.

## Metrics
- `GET /api/stats`: games tracked, listings, snapshots, per-store 7-day job success rate
- `GET /metrics`: Prometheus format (request counts and latency by route, data gauges)

## Admin
`/api/admin/*` needs header `X-Admin-Token: <ADMIN_TOKEN>`; with no token configured it is disabled.

## Without Docker
    cd price-match && pip install -r requirements.txt
    FRONTEND_DIR=../frontend uvicorn app.main:app --reload
    python -m app.snapshot --seed
    pytest

## Known limits
- History conversion uses the latest FX rate, not the rate at capture time.
- Steam/Epic connectors depend on unofficial endpoints and can break without notice.
- The daily job only prices the seed games on Epic. Other games get an Epic price when someone opens the game in the UI, so their Epic history has gaps.
- GOG links for catalog games are title-only: a game whose exact normalized title is shared with other Steam games is queued for review instead of linking automatically, and a GOG product that already belongs to one game is never attached to another.
- A game delisted from Steam keeps its last stored price. The profile shows when each store was last checked and flags a price older than 14 days, so a stale price is visible rather than silent.
- Two people opening the same never-checked game at the same instant can trigger two Epic searches (the global cap still applies).
