# PlayMatch: where we stopped

Written 2026-09-21, at the point a Claude usage limit interrupted the build. Branch: `steam-catalog` (main is still the baseline commit `5ee363b`).

## What PlayMatch is now

A game price tracker: FastAPI service + Postgres + a static web UI. The web UI was redesigned (sticky nav, games grid with cover art, profile pages with a dated price-history chart). The current work adds **every Steam game** to the catalog, with Steam prices refreshed daily in batches, GOG prices from GOG's daily dump, and Epic prices looked up on demand.

Design: `docs/superpowers/specs/2026-09-21-steam-catalog-design.md`
Plans: `docs/superpowers/plans/2026-09-21-steam-catalog-pipeline.md` (Plan 1) and `docs/superpowers/plans/2026-09-21-epic-on-demand-and-ui.md` (Plan 2)

## Done and reviewed (every task had a spec + quality review, most needed fix rounds)

**Plan 1, the data pipeline: complete** (9 tasks plus one final fix wave, whole-branch review done)
- `catalog.py` lists Steam base games; `bulk.py` prices them in batches, resumable, with an outage bailout; snapshots are stored only on change plus a weekly heartbeat; a new `price_checks` table records when a store was last asked.
- `snapshot.py` runs the daily job: catalog, FX, bulk Steam, Epic for the 10 seed games only, GOG import. Each step is isolated so one failure does not stop the others.
- GOG review queue is capped per run and de-duplicated; same-title games are never auto-linked to one GOG product.
- `scripts/scale_test.py` benchmarks 100,000 games: search under 80 ms, GOG matching projects to about 89 s, so no extra index or matching change was needed.

**Plan 2, Epic on demand and UI: 3 of 6 tasks done**
- Task 1: `GET /api/games` is paged (`limit`, `offset`, `X-Total-Count` header).
- Task 2: the prices response reports when each store was last checked (`checks`, `checked_at`).
- Task 3: `POST /api/games/{id}/epic-check` with a per-game cooldown, a global rate cap and a 15-minute block when Epic rate limits us.

Last full test run: 114 passed (see the flaky test below).

## Not done yet, in order

1. **Task 4: "Load more" on the games list**, plus `scripts/seed_demo.py` and `scripts/browser_check.py` (Playwright checks). This was interrupted. The two script files exist as **untracked, unreviewed drafts** in `price-match/scripts/`; the frontend edits (`frontend/app.js`, `frontend/app.css`) were not started. They are deliberately NOT part of this commit.
2. **Task 5:** profile page shows an "Epic price: checking..." row and calls the new endpoint; each store price shows "Checked <date>" and warns when older than 14 days.
3. **Task 6:** README API notes, browser-check instructions, final verification.
4. A final whole-branch review of Plan 2, then a decision on merging `steam-catalog` into `main` (one merge, after both plans).

The plan files hold the exact code, tests and commands for each of those tasks.

## Before this is used for real

- **Steam API key:** not yet used. Catalog sync needs a free key in `.env` as `STEAM_API_KEY`. The first real run must confirm Steam's response shape and find the largest working batch size (`python -m app.bulk --probe`); steps are in the README ("First run with a key"). Do not enable the key on a real deployment until Plan 2's UI is finished, otherwise the list shows only the first 50 of about 100,000 games.
- **Steam rate limits are unmeasured.** If Steam throttles, the daily run stops and resumes next day, so a first full pass may take several days.

## Known problems (not fixed)

- **Flaky test:** `test_history_stats_and_historical_low` fails about half the runs on Windows (three snapshots created in a row get identical timestamps). It existed before this work; it is a test problem, not a product problem.
- **FX rates cannot update:** `api.frankfurter.app` now redirects (HTTP 301) and `fx.refresh` does not follow redirects, so exchange rates are never refreshed. This predates this work. US/USD is unaffected; currency conversion for other currencies needs a fix (new URL `api.frankfurter.dev/v1/latest` plus following redirects).
- Frontend still says "Samples are recorded once a day", and the stats table averages are sample-weighted, which is skewed now that Steam prices are stored only on change.
- Smaller deferred items are listed at the bottom.

## Decisions made on your behalf during the build (each one is reversible)

1. Work stayed on the branch `steam-catalog`; nothing merges to `main` until both plans finish.
2. Subagent commits by the cheap model tier carry a "Claude Haiku 4.5" co-author trailer instead of "Claude Sonnet 5" (it names the model that wrote them). Uniform attribution would need a history rewrite.
3. API key is hidden in HTTP logs (`key=REDACTED`).
4. `scale_test.py populate` refuses to run unless `DATABASE_URL` is set (it inserts 100,000 fake games).
5. The GOG review cap counts only candidates not already queued, so old entries never use it up.
6. The Steam bulk run aborts after 10 failed requests in a row (a real outage), and leaves the rest for next run.
7. "Last checked" shows the later of the last price check and the newest snapshot.
8. `epic-check` commits before the Epic call so a slow Epic does not hold a database connection.
9. FX redirect problem reported, not fixed (out of scope).

## Deferred smaller items (follow-ups)

- One `ok` job row is logged per Steam batch (about 2,000 a day); it skews the per-store success rate.
- No run lock: a manual `docker compose run snapshot` during the scheduled run can collide.
- `python -m app.gogdb` (manual GOG import) is uncapped.
- `Gate.allow()` in `epic_lookup.py` is not thread-safe (bounded over-admission); outcomes other than the four known strings would be treated as success.
- A game title with dozens of `&` could overflow the normalized-title column on Postgres and make catalog sync skip; a non-advancing catalog cursor is guarded, a string-typed cursor would only degrade to a logged skip.
- README benchmark commands use POSIX `VAR=value` syntax (PowerShell needs `$env:`).

## How to resume

The build ran with per-task reviews. The working notes (task ledgers, briefs and review packages) live in `.superpowers/` inside your working copy; that folder is git-ignored so it is **not** on GitHub. If it is gone, use this file plus the two plan files: continue at Plan 2, Task 4.

Test environment used here (Windows): a Python venv with `price-match/requirements.txt`, `pytest`, `numpy` and `playwright` (then `python -m playwright install chromium`). Run tests from `price-match/` with `python -m pytest -q`. Browser checks and the demo database are described in the Plan 2 README section once Task 6 lands.
