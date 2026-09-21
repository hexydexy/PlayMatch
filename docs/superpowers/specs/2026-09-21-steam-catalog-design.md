# Full Steam catalog for PlayMatch: design

Date: 2026-09-21. Status: draft for review. Builds on PlayMatch v1_2.

## Goal

Every Steam base game is searchable in PlayMatch and has a profile page. Each game gets a daily Steam price history, a GOG price wherever the existing GOG dump matches it, and an Epic price fetched when someone opens the game.

Success means:
- A user can search for any Steam base game and open its profile.
- The daily job finishes comfortably inside 24 hours at 100,000+ games.
- Search and the list stay fast at that size.
- A price shown as current is never silently out of date.

## Non-goals

- Regions other than US (each extra region multiplies outbound requests).
- DLC, soundtracks, software, demos, and videos.
- Bulk Epic prices (Epic has no bulk method in this codebase; see Epic on demand).
- Fixing the flaky `test_history_stats_and_historical_low` (unrelated; noted only).
- Caching cover art (still hotlinked from Steam's CDN).

## Decisions already made with the user

- Scope: all of Steam, Epic on demand. Steam prices for all games via batched requests. GOG for all games from the existing daily dump. Epic only when a game is opened, plus the seed games daily.
- Free-to-play and unreleased games are listed but have no price rows, because Steam returns no price for them.
- Snapshots are stored only when a price changes, plus a heartbeat every 7 days. Daily snapshots for 100,000 games would be about 36 million rows a year.
- US region only.
- The user supplies a free Steam Web API key as `STEAM_API_KEY`.

"Seed games" means the entries in `price-match/app/seed_games.json` (currently 10), identified by `steam_app_id`. They are the only games whose Epic price is refreshed by the daily job.

## Facts checked before designing (2026-09-21)

- `ISteamApps/GetAppList/v2` (no key) returns 404. `IStoreService/GetAppList/v1` returns 403 without a key.
- `store.steampowered.com/api/appdetails?appids=<a,b,c>&filters=price_overview&cc=us` returned prices for 10 of 10 ids in one request. Larger batch sizes are untested.
- The total number of Steam base games is unmeasured (it needs the key).
- GOG already imports a single daily dump covering its whole catalog.

## Architecture

The daily job in `snapshot.py` changes from "for every game, ask Steam and Epic" to a pipeline of bulk steps:

1. Catalog sync (new): `catalog.sync()` upserts every Steam base game into `games`.
2. FX refresh (unchanged).
3. Bulk Steam prices (new): batched, resumable, change-only snapshots.
4. Epic for seed games only (existing `ingest_game` with the Epic connector).
5. GOG dump import (existing, adjusted for scale).

Epic for all other games happens on demand through a new API endpoint. The request path never depends on the daily job.

### 1. Catalog sync (`app/catalog.py`)

- Pages through `IStoreService/GetAppList/v1` with `key`, `include_games=true`, `include_dlc=false`, `include_software=false`, `include_videos=false`, `include_hardware=false`, `max_results=50000`, following `last_appid` while `have_more_results` is true.
- For each app: if `steam_app_id` exists, update `title` and `norm_title` when the name changed; otherwise insert a `games` row with `release_year` null. Apps with an empty name are skipped.
- The response parser is a separate function. The response shape above is from Steam's documentation and cannot be checked without a key, so the first task with the key is to confirm it against a real response.
- No `STEAM_API_KEY`: log one warning, skip the step, and continue the job. A 403 or network error is logged and skipped the same way. Catalog sync failing never blocks pricing.
- Runs daily. It is a handful of requests.

### 2. Bulk Steam prices

- New `SteamConnector.prices_for(app_ids, region) -> dict[app_id, RawListing | None]`, using `appdetails` with `filters=price_overview` and several `appids` per request.
- Batch size: `STEAM_BATCH_SIZE`, default 50, and the implementation probes upward to 100 once and reports the working maximum. On a malformed or oversized response the batch is halved and retried. Requests stay spaced by `REQUEST_DELAY`.
- Order: games with no `price_checks` row for `(steam, US)` first, then oldest `checked_at`. An interrupted run therefore resumes where it stopped.
- For each returned price: `get_or_create_listing`, then `record_snapshot_if_due`: skip when the latest snapshot has the same price, base price and currency and is younger than `SNAPSHOT_HEARTBEAT_DAYS` (default 7).
- After each batch, upsert `price_checks(game_id, 'steam', region, checked_at=now)` for every id in it, including ids that returned no price (free, unreleased, delisted), so they are not retried forever.
- Rate limiting: HTTP 429 or 403 raises `RateLimited`. The existing circuit breaker stops Steam for the rest of the run and logs a `JobRun` row. Transport errors on a batch do not write `price_checks`, so the batch is retried next run.
- Seed games are priced by this same path. The old per-game Steam call in the daily loop is removed.

### 3. GOG at scale

- `import_archive` currently runs `process.extract` for every game in the database. At 100,000 games this is untested.
- A benchmark task comes first: 100,000 fake titles against a realistic GOG pool. Decision rule: if the import takes more than 10 minutes, switch to chunked `process.cdist`; otherwise leave the matching as is.
- Review queue: a bulk run auto-matches only. Near-matches (the review band) are queued up to `REVIEW_QUEUE_CAP_PER_RUN` (default 200, highest scores first) so 100,000 games cannot flood the manual queue.

### 4. Epic on demand

- New table `price_checks(game_id, store_id, region, checked_at)`, primary key `(game_id, store_id, region)`. It serves the Epic cooldown, Steam resume ordering, and the "last checked" display. It is created by `create_all`, so no existing table is altered.
- New endpoint `POST /api/games/{id}/epic-check?region=US` (public). Response `{"status": ..., "checked_at": ...}` with status one of:
  - `fresh`: checked within `EPIC_COOLDOWN_HOURS` (default 24). No outbound request.
  - `checked`: Epic was queried. New snapshots, if any, are stored through the existing matcher.
  - `busy`: the global limiter (`EPIC_ON_DEMAND_PER_MINUTE`, default 30, in-process token bucket) is exhausted.
  - `unavailable`: Epic rate limited us. Epic is then blocked for 15 minutes.
- A query that finds no confident match still writes `price_checks` and a `no_match` `JobRun`, so a game is not searched again for 24 hours.
- The limiter is per process. With one API process (the current compose setup) this holds. Multiple workers would need a shared limiter; that is out of scope and noted in the README.
- `GET /api/games/{id}/prices` adds `checked_at` per store: `steam` and `epic` from `price_checks`, and `gog` from the latest GOG snapshot time.
- The daily job still refreshes Epic for the seed games. Epic history for other games therefore grows only when someone opens them, and is sparse.

### 5. API and UI

- `GET /api/games` gains `offset` (default 0) and `limit` (default 20, maximum 100). Response body stays a list. The total match count is returned in the `X-Total-Count` header, and CORS exposes it.
- List page: shows 48 games and a "Load more" button that fetches the next page. The count line reads "12,345 games" for the whole match total, not just what is loaded. Search resets to the first page.
- Profile page:
  - After the prices load, if there is no Epic `checked_at` or it is older than 24 hours, the page posts `epic-check`. While waiting, the prices table shows an "Epic Games: checking…" row. On `checked` the prices are re-fetched. On `busy` or `unavailable` the row says the Epic price is unavailable right now.
  - Each store row shows "checked <date>". If a store's `checked_at` is more than 14 days old, the row says the price may be out of date. This is needed because a change-only snapshot's timestamp is the last change, not the last check.
- No change to the chart, nav, or visual design.

### 6. Configuration

New variables, all optional except the key for catalog sync: `STEAM_API_KEY`, `STEAM_BATCH_SIZE`, `SNAPSHOT_HEARTBEAT_DAYS`, `EPIC_ON_DEMAND_PER_MINUTE`, `EPIC_COOLDOWN_HOURS`, `REVIEW_QUEUE_CAP_PER_RUN`. Added to `.env.example`, passed through `docker-compose.yml` to the `price-match`, `scheduler` and `snapshot` services, and documented in the README.

## Error handling summary

| Situation | Behavior |
| --- | --- |
| No or invalid `STEAM_API_KEY` | Catalog sync skipped with a logged reason; app keeps working on existing games |
| Steam rate limits a batch | Circuit breaker stops Steam for the run; unchecked games are first in line next run |
| Batch transport error | No `price_checks` written; retried next run |
| Game delisted from Steam | No new snapshots; profile shows the last price with a "may be out of date" note after 14 days |
| Epic rate limits on demand | `unavailable` for 15 minutes; profile shows the note |
| Epic global limit exceeded | `busy`; profile shows the note; retried the next time the game is opened |

## Testing

- Unit tests with faked HTTP: catalog paging and cursor, name-change update, empty-name skip, no-key skip; batch parsing including ids with no price and malformed responses; batch halving; `record_snapshot_if_due` (same price inside and outside the heartbeat, changed price); resume ordering.
- API tests: `offset` and `limit` bounds, `X-Total-Count`, `epic-check` for each status, cooldown, and the limiter, using an injected fake Epic connector and clock.
- Scale test (script, not part of the default suite): 100,000 fake games in Postgres, timing search and the GOG import. Decision rules: if a `contains` search takes more than 200 ms, add a `pg_trgm` index on `norm_title` (Postgres only); if the GOG import takes more than 10 minutes, use chunked `cdist`.
- Browser check with Playwright: list paging, search reset, profile Epic states (checking, fresh, unavailable), stale-price note.
- Real-key verification, done by the user: confirm the catalog response shape and the largest working `STEAM_BATCH_SIZE`, and report the game count.

## Risks

- Steam's `appdetails` is unofficial and may throttle or change without notice. The circuit breaker limits the damage to one skipped run.
- Steam's terms apply to bulk use of its endpoints. The load is about 1,000 requests a day at batch size 100, but the user should check they are comfortable with it.
- Catalog and batch behavior at full scale is unmeasured until a real key is used.
- Sparse Epic history for non-seed games is inherent to on-demand fetching.
