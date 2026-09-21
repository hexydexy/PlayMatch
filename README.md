# PlayMatch (v1)

Price match and price history for PC games across Steam, GOG and Epic Games. US region first; other regions are a config change.

## Layout
- `price-match/` FastAPI service: connectors, matcher, FX, GOGDB importer, daily job, price + history + stats API, `/metrics`
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

## Data sources
| Store | How | Notes |
| --- | --- | --- |
| Steam | Public storefront endpoints, live | Unofficial. 1 request/second by default |
| Epic | Storefront GraphQL, live | Unofficial. Same politeness settings |
| GOG | GOGDB daily dump (gogdb.org/backups_v3), one ~60 MB download per day | Also backfills real price history. No requests to GOG itself |
| G2A | Dropped: no open API and scraping would breach terms | |

Rate-limit safety: requests are spaced by `REQUEST_DELAY`; on the first HTTP 429/403 from a store the job stops calling that store for the rest of the run and records `rate_limited`. Never raise concurrency; lower it if anything.

### GOGDB layout caveat
The dump's internal layout could not be checked when this was written. The importer searches for `<id>/product.json` and `<id>/prices.json` and accepts several price shapes. If the log shows `no prices parsed`, run:

    docker compose run --rm --entrypoint python snapshot -m app.gogdb --inspect /cache/gogdb_YYYY-MM-DD.tar.xz

and adjust `app/gogdb.py` (`extract_prices`). Also check that prices come out right (cents vs dollars).
GOG history is change-point data (one record per price change) while Steam/Epic are daily samples, so per-store averages are not directly comparable.

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
