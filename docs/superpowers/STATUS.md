# PlayMatch: where we stopped

Written 2026-09-21 when a Claude usage limit interrupted the build, and updated later the same day in a second session that finished Plan 2's remaining tasks. Branch: `steam-catalog` (`main` is still the original baseline commit, before any of this work).

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

**Plan 2, Epic on demand and UI: all 6 tasks implemented** (Tasks 1-3 reviewed; Tasks 4-6 are not independently reviewed — see below)
- Task 1: `GET /api/games` is paged (`limit`, `offset`, `X-Total-Count` header).
- Task 2: the prices response reports when each store was last checked (`checks`, `checked_at`).
- Task 3: `POST /api/games/{id}/epic-check` with a per-game cooldown, a global rate cap and a 15-minute block when Epic rate limits us.
- Task 4: the list loads 48 games a page with a "Load more" button and the whole-catalog count; a "Load more" that loses a race with a newer search is discarded. Added `scripts/seed_demo.py` (124-game demo database) and `scripts/browser_check.py` (Playwright).
- Task 5: opening a game not checked in the last day shows a "Checking the Epic price…" row and calls the endpoint; `checked`/`fresh` re-renders quietly with the new prices, `busy`/`unavailable` explain in place. Each store row shows "Checked <date>" and flags a price over 14 days old.
- Task 6: README API notes and browser-check instructions; two stale "Known limits" bullets corrected.

Last full test run: 114 collected, 113 passed plus the known flake below. All 17 browser checks pass against a fresh demo database.

## Not done yet, in order

1. **Plan 2 (Tasks 1-6) is fully implemented.** `docker compose build` succeeded for all three images (`price-match`, `scheduler`, `frontend`) once Docker Desktop was started; the demo server and `price-match/demo.db` were cleaned up afterwards.
2. **Tasks 4, 5 and 6 were implemented inline without the implementer/reviewer subagent pair the earlier tasks used, so they had no independent task review as they landed.** A final whole-branch review is in progress to cover them, dispatched as two parallel subagents (frontend + scripts; backend + tests/README/Docker/gitignore), base `6cb22e8` (the commit `main` is still on) to the branch head. If this file still says "in progress" when you read it, check whether that review finished; if it did, its verdict and any fix rounds it triggered should be folded into this file before merging.
3. Once the review is clean, merge `steam-catalog` into `main` (one merge, after both plans, as decided at the start of this work).

The plan files hold the exact code, tests and commands for each task.

## Rebuilding the test environment

The venv from the first session was gone by the second. It is now at `./.venv` (git-ignored) and was built with:

    python -m venv .venv
    ./.venv/Scripts/python.exe -m pip install -r price-match/requirements.txt numpy playwright
    ./.venv/Scripts/python.exe -m playwright install chromium

Run tests from `price-match/` with `../.venv/Scripts/python.exe -m pytest -q`.

## Before this is used for real

- **Steam API key:** not yet used. Catalog sync needs a free key in `.env` as `STEAM_API_KEY`. The first real run must confirm Steam's response shape and find the largest working batch size (`python -m app.bulk --probe`); steps are in the README ("First run with a key"). The UI blocker on this is now gone: the list pages through the whole catalog.
- **Steam rate limits are unmeasured.** If Steam throttles, the daily run stops and resumes next day, so a first full pass may take several days.

## Known problems (not fixed)

- **Flaky test:** `test_history_stats_and_historical_low` fails on Windows — sometimes about half the runs, and in the second session five runs in a row. It existed before this work and no backend file has changed since, so it is not a regression. Cause, now pinned down: the test records three snapshots (2000, 1000, 1500) back to back, Windows clock resolution gives them one identical timestamp, and `current_prices` picks the newest snapshot by time with no id tiebreaker, so it can return the 1000 sample and report `at_historical_low` as true. Real snapshots are a day apart, so it does not affect the product; ordering by `(created_at DESC, id DESC)` would fix both the test and the latent ambiguity.
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
