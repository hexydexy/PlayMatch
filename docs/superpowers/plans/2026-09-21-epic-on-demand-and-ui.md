# Epic On Demand, API Paging and UI Implementation Plan (Plan 2 of 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** With tens of thousands of games in the catalog, the list is paged, opening a game fetches its Epic price when stale, and every price shows when it was last checked.

**Architecture:** `GET /api/games` gains `offset` and an `X-Total-Count` header. `GET /api/games/{id}/prices` gains per-store `checked_at` and a top-level `checks` map. A new `epic_lookup` module and `POST /api/games/{id}/epic-check` endpoint run one rate-limited Epic search per game per cooldown. The frontend adds "Load more" and the Epic checking states, and a Playwright script checks the behavior in a real browser.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, pytest; vanilla JS/CSS frontend; Playwright for browser checks.

**Spec:** `docs/superpowers/specs/2026-09-21-steam-catalog-design.md`. This plan needs Plan 1 (`2026-09-21-steam-catalog-pipeline.md`) finished first: it uses `models.PriceCheck`, `service.mark_checked`, `service.as_utc`, `service.record_snapshot_if_due`, the `config.EPIC_*` settings, `tests/conftest.py` (`db_session` fixture) and `scripts/scale_test.py` (`populate`).

## Global Constraints

- US region only.
- `GET /api/games` response body stays a JSON list; the match total is in the `X-Total-Count` header, which CORS must expose. `limit` default 20 and maximum 100; `offset` default 0.
- `POST /api/games/{id}/epic-check?region=US` is public. Status is one of `fresh`, `checked`, `busy`, `unavailable`. Cooldown `EPIC_COOLDOWN_HOURS` (default 24); global cap `EPIC_ON_DEMAND_PER_MINUTE` (default 30, in-process token bucket, one API process); Epic rate limiting blocks lookups for 15 minutes.
- A lookup that finds no confident match still writes `price_checks`, so the game is not searched again within the cooldown. A lookup that ends in `busy`, `unavailable` or an error does not.
- The list page shows 48 games per page with a "Load more" button; the count line shows the whole match total.
- A store row whose `checked_at` is more than 14 days old says the price may be out of date.
- No change to the chart, nav, colors or typography.
- Run backend tests from `price-match/` with `python -m pytest -q`. `test_history_stats_and_historical_low` is a known flaky test (identical timestamps on Windows); if it is the only failure, rerun it. It is not caused by this work.
- Every commit message ends with the trailer `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>` (second `-m` argument). Skip commit steps only if the project is still not a git repo and the user declined `git init` in Plan 1.
- Frontend copy: sentence case, plain words, an action keeps one name ("Load more"), errors say what to do next.

## Review Focus

Failure modes the spec implies that no single task's happy path exercises. Each has a test in the task named in brackets.

1. `offset` past the end, `limit` outside 1 to 100, and `%`/`_` in the search text must not break the list endpoint or the total. [Task 1]
2. A game Epic does not list (no confident match) must not trigger a fresh Epic search on every visit. [Task 3]
3. Epic rate limiting or an error must not mark the game as checked, and a rate limit must block every lookup for 15 minutes, then release. [Task 3]
4. "Load more" racing with a new search must never mix pages from the old and new query. [Tasks 4, 6]
5. A game with no prices at all and an Epic lookup pending must still show the prices section with "checking"; a store checked more than 14 days ago must say its price may be out of date. [Tasks 5, 6]

Known and accepted, with no test: two people opening the same never-checked game at the same instant can trigger two Epic searches (the global cap still holds).

---

### Task 1: Paged games list API

**Files:**
- Modify: `price-match/tests/conftest.py` (add the `api` fixture)
- Create: `price-match/tests/test_api_games.py`
- Modify: `price-match/app/main.py` (`search_games`, imports, CORS)

**Interfaces:**
- Consumes: `service.game_dict`, `matcher.normalize`, the Plan 1 `db_session` fixture.
- Produces: `GET /api/games?q=&limit=&offset=` returning a list of game dicts ordered by `(title, id)` with header `X-Total-Count`; pytest fixture `api` (a `TestClient` whose app uses the `db_session` database).

- [ ] **Step 1: Add the `api` fixture**

Append to `price-match/tests/conftest.py`:

```python
@pytest.fixture()
def api(db_session):
    """A TestClient whose app reads and writes the same database as `db_session`."""
    from fastapi.testclient import TestClient

    from app import main

    Sess = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)

    def override():
        with Sess() as x:
            yield x

    main.app.dependency_overrides[main.get_session] = override
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()
```

- [ ] **Step 2: Write the failing tests**

Create `price-match/tests/test_api_games.py`:

```python
from app import service


def add_games(s, n):
    for i in range(n):
        service.upsert_game(s, f"Game {i:03d}", 5000 + i)


def test_paging_with_a_total_count_header(api, db_session):
    add_games(db_session, 120)
    r = api.get("/api/games", params={"limit": 48})
    assert r.status_code == 200 and r.headers["X-Total-Count"] == "120"
    assert len(r.json()) == 48 and r.json()[0]["title"] == "Game 000"
    second = api.get("/api/games", params={"limit": 48, "offset": 48}).json()
    assert second[0]["title"] == "Game 048"
    last = api.get("/api/games", params={"limit": 48, "offset": 96}).json()
    assert len(last) == 24


def test_offset_past_the_end_is_an_empty_list_with_the_true_total(api, db_session):
    add_games(db_session, 5)
    r = api.get("/api/games", params={"offset": 500})
    assert r.status_code == 200 and r.json() == [] and r.headers["X-Total-Count"] == "5"


def test_the_total_counts_only_games_matching_the_search(api, db_session):
    for title in ("Hades", "Hades II", "Celeste"):
        service.upsert_game(db_session, title)
    r = api.get("/api/games", params={"q": "hades"})
    assert r.headers["X-Total-Count"] == "2" and len(r.json()) == 2


def test_limit_and_offset_are_validated(api, db_session):
    assert api.get("/api/games", params={"limit": 0}).status_code == 422
    assert api.get("/api/games", params={"limit": 101}).status_code == 422
    assert api.get("/api/games", params={"limit": 100}).status_code == 200
    assert api.get("/api/games", params={"offset": -1}).status_code == 422


def test_default_limit_is_20(api, db_session):
    add_games(db_session, 30)
    assert len(api.get("/api/games").json()) == 20


def test_wildcard_characters_in_the_search_do_not_break_the_endpoint(api, db_session):
    add_games(db_session, 3)
    for q in ("%", "_", "%%", "50%"):
        r = api.get("/api/games", params={"q": q})
        assert r.status_code == 200 and "X-Total-Count" in r.headers


def test_the_total_header_is_readable_from_another_origin(api, db_session):
    add_games(db_session, 1)
    r = api.get("/api/games", headers={"Origin": "http://example.test"})
    assert "x-total-count" in r.headers["access-control-expose-headers"].lower()
```

- [ ] **Step 3: Run to verify failure**

Run: `python -m pytest tests/test_api_games.py -q`
Expected: FAIL (`KeyError: 'X-Total-Count'` and 200 where 422 is expected).

- [ ] **Step 4: Implement**

In `price-match/app/main.py` change the import line `from sqlalchemy import select` to:

```python
from sqlalchemy import func, select
```

Replace the CORS middleware call with:

```python
app.add_middleware(CORSMiddleware, allow_origins=config.CORS_ORIGINS,
                   allow_methods=["*"], allow_headers=["*"], expose_headers=["X-Total-Count"])
```

Replace the whole `search_games` function with:

```python
@app.get("/api/games")
def search_games(response: Response, q: str = Query("", max_length=100),
                 limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
                 s: Session = Depends(get_session)):
    total = select(func.count(Game.id))
    stmt = select(Game).order_by(Game.title, Game.id).limit(limit).offset(offset)
    if q:
        match = Game.norm_title.contains(normalize(q))
        total, stmt = total.where(match), stmt.where(match)
    response.headers["X-Total-Count"] = str(s.scalar(total))
    return [service.game_dict(g) for g in s.scalars(stmt)]
```

- [ ] **Step 5: Run to verify pass**

Run: `python -m pytest tests/test_api_games.py -q` then `python -m pytest -q`
Expected: 7 passed; the existing `test_api_smoke` still passes (the body is still a list).

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: page the games list and return the match total in X-Total-Count" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 2: "Last checked" in the prices response

**Files:**
- Create: `price-match/tests/test_prices_checked.py`
- Modify: `price-match/app/service.py` (`current_prices`)

**Interfaces:**
- Consumes: `models.PriceCheck`, `service.as_utc`, `service.mark_checked`, `service.record_snapshot_if_due`.
- Produces: each row in `current_prices(...)["prices"]` gains `checked_at: str | None` (ISO, with timezone); the result gains `checks: {"steam": str | None, "gog": str | None, "epic": str | None}`. A store's value is its `price_checks` time when there is one, otherwise the time of its latest snapshot, otherwise `None`.

- [ ] **Step 1: Write the failing tests**

Create `price-match/tests/test_prices_checked.py`:

```python
from datetime import datetime, timedelta, timezone

from app import service
from app.connectors.base import RawListing

T = datetime(2026, 9, 1, 6, 0, tzinfo=timezone.utc)


def steam(price=1999):
    return RawListing("steam", "1145360", "Hades", "http://x", price, 2499, "USD", steam_app_id=1145360)


def test_checked_at_falls_back_to_the_latest_snapshot_time(db_session):
    g = service.upsert_game(db_session, "Hades", 1145360, 2020)
    service.record_snapshot_if_due(db_session, g, steam(), T)
    db_session.commit()
    p = service.current_prices(db_session, g, "USD", "US")
    assert p["prices"][0]["checked_at"] == T.isoformat()
    assert p["checks"] == {"steam": T.isoformat(), "gog": None, "epic": None}


def test_a_price_check_is_newer_than_the_snapshot_it_did_not_change(db_session):
    g = service.upsert_game(db_session, "Hades", 1145360, 2020)
    service.record_snapshot_if_due(db_session, g, steam(), T)
    later = T + timedelta(days=3)
    service.mark_checked(db_session, [g.id], "steam", "US", later)
    p = service.current_prices(db_session, g, "USD", "US")
    assert p["prices"][0]["checked_at"] == later.isoformat()
    assert p["checks"]["steam"] == later.isoformat()


def test_a_checked_store_with_no_listing_is_reported_in_checks_but_has_no_row(db_session):
    g = service.upsert_game(db_session, "Hades", 1145360, 2020)
    service.mark_checked(db_session, [g.id], "epic", "US", T)
    p = service.current_prices(db_session, g, "USD", "US")
    assert p["prices"] == [] and p["checks"]["epic"] == T.isoformat()


def test_checks_are_per_region(db_session):
    g = service.upsert_game(db_session, "Hades", 1145360, 2020)
    service.mark_checked(db_session, [g.id], "epic", "GB", T)
    assert service.current_prices(db_session, g, "USD", "US")["checks"]["epic"] is None
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_prices_checked.py -q`
Expected: FAIL (`KeyError: 'checks'`).

- [ ] **Step 3: Implement**

In `price-match/app/service.py`, inside `current_prices`, immediately after `store = s.get(Store, listing.store_id)` add:

```python
        latest_at[store.id] = max(latest_at.get(store.id, snap.captured_at), snap.captured_at)
```

and directly above the `for snap, listing in _latest_snapshots(...)` line add:

```python
    latest_at: dict[str, datetime] = {}
```

Then, immediately before the line `    priced = [r for r in rows if r["price_cents"] is not None]` add:

```python
    checks = {c.store_id: c.checked_at for c in s.scalars(select(PriceCheck).where(
        PriceCheck.game_id == game.id, PriceCheck.region == reg.code))}

    def checked(store_id: str) -> str | None:
        t = checks.get(store_id) or latest_at.get(store_id)
        return as_utc(t).isoformat() if t else None

    for r in rows:
        r["checked_at"] = checked(r["store_id"])
```

and in the returned dict, directly after the line `        "game": game_dict(game), "region": reg.code, "currency": currency,` add:

```python
        "checks": {sid: checked(sid) for sid in ("steam", "gog", "epic")},
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_prices_checked.py -q` then `python -m pytest -q`
Expected: 4 passed; existing tests that read `current_prices` still pass.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: report when each store was last checked in the prices response" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 3: Epic lookup and endpoint

**Files:**
- Create: `price-match/app/epic_lookup.py`
- Create: `price-match/tests/test_epic_lookup.py`
- Create: `price-match/tests/test_epic_api.py`
- Modify: `price-match/app/main.py` (import and endpoint)

**Interfaces:**
- Consumes: `service.ingest_game(s, game, region, connectors=[...], blocked=set())` (returns `{"stores": {"epic": "ok" | "no confident match" | "rate_limited: ..." | "error: ..."}}`), `service.mark_checked`, `service.as_utc`, `models.PriceCheck`, `connectors.epic.EpicConnector`, `config.EPIC_ON_DEMAND_PER_MINUTE`, `config.EPIC_COOLDOWN_HOURS`.
- Produces: `epic_lookup.Gate(per_minute: int | None = None, clock=time.monotonic)` with `blocked() -> bool`, `block() -> None`, `allow() -> bool`; module-level `epic_lookup.default_gate`; `epic_lookup.check_epic(s, game, region, connector=None, gate=None, now=None) -> dict` returning `{"status": "fresh" | "checked" | "busy" | "unavailable", "checked_at": str | None}`; `POST /api/games/{game_id}/epic-check?region=`.

- [ ] **Step 1: Write the failing lookup tests**

Create `price-match/tests/test_epic_lookup.py`:

```python
from datetime import datetime, timedelta, timezone

from app import config, epic_lookup, service
from app.connectors.base import ConnectorError, RateLimited, RawListing
from app.models import JobRun, PriceCheck, PriceSnapshot

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


class FakeEpic:
    store_id = "epic"

    def __init__(self, result=None, exc=None):
        self.result, self.exc, self.calls = result or [], exc, 0

    def search(self, title, region="US"):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.result


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def epic_hades(price=1999):
    return RawListing("epic", "hades-epic", "Hades", "https://store.epicgames.com/p/hades",
                      price, 2499, "USD", release_year=2020)


def new_gate(per_minute=30, clock=None):
    return epic_lookup.Gate(per_minute, clock or Clock())


def hades(s):
    return service.upsert_game(s, "Hades", 1145360, 2020)


def check_count(s):
    return s.query(PriceCheck).filter_by(store_id="epic").count()


def test_a_lookup_stores_the_price_and_marks_the_game_checked(db_session):
    g, fake = hades(db_session), FakeEpic([epic_hades()])
    out = epic_lookup.check_epic(db_session, g, "US", connector=fake, gate=new_gate(), now=NOW)
    assert out == {"status": "checked", "checked_at": NOW.isoformat()} and fake.calls == 1
    assert db_session.query(PriceSnapshot).one().price_cents == 1999
    assert check_count(db_session) == 1


def test_a_recent_check_is_fresh_and_makes_no_request(db_session):
    g, fake = hades(db_session), FakeEpic([epic_hades()])
    service.mark_checked(db_session, [g.id], "epic", "US", NOW - timedelta(hours=1))
    out = epic_lookup.check_epic(db_session, g, "US", connector=fake, gate=new_gate(), now=NOW)
    assert out["status"] == "fresh" and fake.calls == 0


def test_a_check_older_than_the_cooldown_looks_again(db_session):
    g, fake = hades(db_session), FakeEpic([epic_hades()])
    service.mark_checked(db_session, [g.id], "epic", "US", NOW - timedelta(hours=config.EPIC_COOLDOWN_HOURS, minutes=1))
    out = epic_lookup.check_epic(db_session, g, "US", connector=fake, gate=new_gate(), now=NOW)
    assert out["status"] == "checked" and fake.calls == 1


def test_no_confident_match_is_still_marked_checked_so_the_next_visit_is_fresh(db_session):
    g = hades(db_session)
    other = RawListing("epic", "x", "Completely Different Game", "http://x", 100, 100, "USD")
    fake = FakeEpic([other])
    first = epic_lookup.check_epic(db_session, g, "US", connector=fake, gate=new_gate(), now=NOW)
    assert first["status"] == "checked" and db_session.query(PriceSnapshot).count() == 0
    second = epic_lookup.check_epic(db_session, g, "US", connector=fake, gate=new_gate(), now=NOW + timedelta(minutes=5))
    assert second["status"] == "fresh" and fake.calls == 1


def test_the_global_cap_returns_busy_without_marking_checked_and_refills_over_time(db_session):
    clock = Clock()
    gate = new_gate(per_minute=1, clock=clock)
    g1 = hades(db_session)
    g2 = service.upsert_game(db_session, "Celeste", 504230, 2018)
    fake = FakeEpic([])
    assert epic_lookup.check_epic(db_session, g1, "US", connector=fake, gate=gate, now=NOW)["status"] == "checked"
    busy = epic_lookup.check_epic(db_session, g2, "US", connector=fake, gate=gate, now=NOW)
    assert busy["status"] == "busy" and fake.calls == 1 and check_count(db_session) == 1
    clock.t += 61
    assert epic_lookup.check_epic(db_session, g2, "US", connector=fake, gate=gate, now=NOW)["status"] == "checked"


def test_rate_limiting_blocks_all_lookups_for_fifteen_minutes_then_releases(db_session):
    clock = Clock()
    gate = new_gate(clock=clock)
    g1 = hades(db_session)
    g2 = service.upsert_game(db_session, "Celeste", 504230, 2018)
    limited = FakeEpic(exc=RateLimited("epic rate limited (HTTP 429)"))
    out = epic_lookup.check_epic(db_session, g1, "US", connector=limited, gate=gate, now=NOW)
    assert out["status"] == "unavailable" and check_count(db_session) == 0
    assert db_session.query(JobRun).filter_by(store_id="epic", outcome="rate_limited").count() == 1
    healthy = FakeEpic([])
    assert epic_lookup.check_epic(db_session, g2, "US", connector=healthy, gate=gate, now=NOW)["status"] == "unavailable"
    assert healthy.calls == 0  # blocked: no request was made
    clock.t += 15 * 60 + 1
    assert epic_lookup.check_epic(db_session, g2, "US", connector=healthy, gate=gate, now=NOW)["status"] == "checked"


def test_an_error_is_unavailable_not_marked_checked_and_does_not_block(db_session):
    g = hades(db_session)
    broken = FakeEpic(exc=ConnectorError("epic HTTP 500"))
    gate = new_gate()
    assert epic_lookup.check_epic(db_session, g, "US", connector=broken, gate=gate, now=NOW)["status"] == "unavailable"
    assert check_count(db_session) == 0
    assert epic_lookup.check_epic(db_session, g, "US", connector=broken, gate=gate, now=NOW)["status"] == "unavailable"
    assert broken.calls == 2  # not blocked: the next visit tries again
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_epic_lookup.py -q`
Expected: FAIL (`ImportError: cannot import name 'epic_lookup'`).

- [ ] **Step 3: Implement the lookup**

Create `price-match/app/epic_lookup.py`:

```python
"""Epic price lookup for one game, triggered when someone opens it.

Epic has no bulk method here, so non-seed games are looked up on demand. Three limits
protect Epic (and us): a per-game cooldown (a `price_checks` row), a global token bucket,
and a cool-off after Epic rate limits us.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta

from sqlalchemy.orm import Session

from . import config, service
from .connectors.epic import EpicConnector
from .models import Game, PriceCheck, utcnow

log = logging.getLogger("playmatch.epic")
BLOCK_SECONDS = 15 * 60


class Gate:
    """Global limits for on-demand Epic lookups. Per process, so it holds for one API worker."""

    def __init__(self, per_minute: int | None = None, clock=time.monotonic):
        self.per_minute = config.EPIC_ON_DEMAND_PER_MINUTE if per_minute is None else per_minute
        self.clock = clock
        self.tokens = float(self.per_minute)
        self.last = clock()
        self.blocked_until = 0.0

    def blocked(self) -> bool:
        return self.clock() < self.blocked_until

    def block(self) -> None:
        self.blocked_until = self.clock() + BLOCK_SECONDS

    def allow(self) -> bool:
        now = self.clock()
        self.tokens = min(float(self.per_minute), self.tokens + (now - self.last) * self.per_minute / 60)
        self.last = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


default_gate = Gate()


def _iso(row: PriceCheck | None) -> str | None:
    return service.as_utc(row.checked_at).isoformat() if row else None


def check_epic(s: Session, game: Game, region: str, connector=None, gate: Gate | None = None,
               now=None) -> dict:
    gate = gate or default_gate
    now = now or utcnow()
    row = s.get(PriceCheck, (game.id, "epic", region))
    if row and now - service.as_utc(row.checked_at) < timedelta(hours=config.EPIC_COOLDOWN_HOURS):
        return {"status": "fresh", "checked_at": _iso(row)}
    if gate.blocked():
        return {"status": "unavailable", "checked_at": _iso(row)}
    if not gate.allow():
        return {"status": "busy", "checked_at": _iso(row)}
    report = service.ingest_game(s, game, region, connectors=[connector or EpicConnector()], blocked=set())
    outcome = report["stores"].get("epic", "")
    if outcome.startswith("rate_limited"):
        gate.block()
        return {"status": "unavailable", "checked_at": _iso(row)}
    if outcome.startswith("error"):
        return {"status": "unavailable", "checked_at": _iso(row)}
    service.mark_checked(s, [game.id], "epic", region, now)
    return {"status": "checked", "checked_at": service.as_utc(now).isoformat()}
```

- [ ] **Step 4: Run to verify the lookup tests pass**

Run: `python -m pytest tests/test_epic_lookup.py -q`
Expected: 7 passed.

- [ ] **Step 5: Write the failing endpoint tests**

Create `price-match/tests/test_epic_api.py`:

```python
from app import epic_lookup, service
from tests.test_epic_lookup import FakeEpic, epic_hades, new_gate


def wire(monkeypatch, fake):
    monkeypatch.setattr(epic_lookup, "EpicConnector", lambda: fake)
    monkeypatch.setattr(epic_lookup, "default_gate", new_gate())


def test_epic_check_stores_the_price_then_reports_fresh(api, db_session, monkeypatch):
    g = service.upsert_game(db_session, "Hades", 1145360, 2020)
    fake = FakeEpic([epic_hades()])
    wire(monkeypatch, fake)
    first = api.post(f"/api/games/{g.id}/epic-check")
    assert first.status_code == 200 and first.json()["status"] == "checked"
    assert api.post(f"/api/games/{g.id}/epic-check").json()["status"] == "fresh"
    assert fake.calls == 1
    prices = api.get(f"/api/games/{g.id}/prices").json()
    assert prices["checks"]["epic"] is not None
    assert [p["store_id"] for p in prices["prices"]] == ["epic"]


def test_epic_check_for_an_unknown_game_is_404(api, monkeypatch):
    wire(monkeypatch, FakeEpic())
    assert api.post("/api/games/999/epic-check").status_code == 404


def test_epic_check_for_a_disabled_region_is_400(api, db_session, monkeypatch):
    g = service.upsert_game(db_session, "Hades", 1145360, 2020)
    wire(monkeypatch, FakeEpic())
    assert api.post(f"/api/games/{g.id}/epic-check?region=JP").status_code == 400
```

- [ ] **Step 6: Run to verify failure**

Run: `python -m pytest tests/test_epic_api.py -q`
Expected: FAIL (405 Method Not Allowed: the route does not exist).

- [ ] **Step 7: Add the endpoint**

In `price-match/app/main.py` change the import to `from . import config, epic_lookup, fx, metrics, regions, service` and add after the `history` endpoint:

```python
@app.post("/api/games/{game_id}/epic-check")
def epic_check(game_id: int, region: str | None = None, s: Session = Depends(get_session)):
    """Look up the Epic price for this game if it has not been checked recently."""
    return epic_lookup.check_epic(s, _game(s, game_id), _region(region))
```

- [ ] **Step 8: Run to verify pass**

Run: `python -m pytest tests/test_epic_api.py tests/test_epic_lookup.py -q` then `python -m pytest -q`
Expected: 10 passed; full suite green apart from the known flaky test.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "feat: look up the Epic price when a game is opened, with cooldown and rate limits" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 4: "Load more" on the games list, with browser checks

**Files:**
- Create: `price-match/scripts/seed_demo.py`
- Create: `price-match/scripts/browser_check.py`
- Modify: `frontend/app.js` (the games list section, `state`)
- Modify: `frontend/app.css` (`.grid`, new `.more-wrap`)

**Interfaces:**
- Consumes: `GET /api/games?limit&offset&q` with `X-Total-Count` (Task 1); `scripts.scale_test.populate(n)` (Plan 1).
- Produces: `scripts/seed_demo.py` (demo database for browser checks: 124 games including Hades with a fresh Epic check, "Fresh Steam Only", "Old Price Game" and "Free Game"); `scripts/browser_check.py` with `check(name, ok, detail)` and `check_list(page)`; the paged list UI.

- [ ] **Step 1: Install Playwright (once)**

```bash
pip install playwright
python -m playwright install chromium
```

Expected: both succeed. (Skip if already installed.)

- [ ] **Step 2: Create the demo data script**

Create `price-match/scripts/seed_demo.py`:

```python
"""Demo data for the browser checks. Run from price-match/ with DATABASE_URL set:

    python -m scripts.seed_demo

Creates 120 filler games plus four named ones (124 in total):
  Hades              Steam price, Steam and Epic both checked just now
  Fresh Steam Only   Steam price, Steam checked now, Epic never checked
  Old Price Game     Steam price last checked 30 days ago, Epic checked now
  Free Game          no prices, nothing checked
"""
from datetime import timedelta

from app import service
from app.connectors.base import RawListing
from app.db import SessionLocal, init_db
from app.models import utcnow
from scripts import scale_test


def steam(app_id, title, price):
    return RawListing("steam", str(app_id), title, f"https://store.steampowered.com/app/{app_id}",
                      price, price + 1000, "USD", steam_app_id=app_id)


def main():
    init_db()
    scale_test.populate(120)
    now = utcnow()
    with SessionLocal() as s:
        service.seed_stores(s)
        hades = service.upsert_game(s, "Hades", 1145360, 2020)
        service.record_snapshot_if_due(s, hades, steam(1145360, "Hades", 1499), now)
        s.commit()
        service.mark_checked(s, [hades.id], "steam", "US", now)
        service.mark_checked(s, [hades.id], "epic", "US", now)

        fresh = service.upsert_game(s, "Fresh Steam Only", 8000001, 2024)
        service.record_snapshot_if_due(s, fresh, steam(8000001, "Fresh Steam Only", 999), now)
        s.commit()
        service.mark_checked(s, [fresh.id], "steam", "US", now)

        old = service.upsert_game(s, "Old Price Game", 8000002, 2019)
        long_ago = now - timedelta(days=30)
        service.record_snapshot_if_due(s, old, steam(8000002, "Old Price Game", 499), long_ago)
        s.commit()
        service.mark_checked(s, [old.id], "steam", "US", long_ago)
        service.mark_checked(s, [old.id], "epic", "US", now)

        service.upsert_game(s, "Free Game", 8000003, 2023)
    print("demo data ready")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Create the browser check with the list checks (failing first)**

Create `price-match/scripts/browser_check.py`:

```python
"""Browser checks for the catalog UI. Needs the app running with the demo data:

    (from price-match/)
    $env:DATABASE_URL = "sqlite:///demo.db"        # bash: export DATABASE_URL=sqlite:///demo.db
    python -m scripts.seed_demo
    $env:FRONTEND_DIR = "../frontend"
    python -m uvicorn app.main:app --port 8765
    # in another terminal:
    python -m scripts.browser_check http://localhost:8765/

Every /epic-check call is intercepted, so nothing here contacts Epic. Exit code 1 if any check fails.
"""
import json
import sys

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8765/"
failures = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def cards(p):
    return p.locator("#grid .game").count()


def wait_cards(p, n):
    p.wait_for_function(f"document.querySelectorAll('#grid .game').length === {n}")


def check_list(p):
    p.goto(BASE)
    p.wait_for_selector("#grid .game")
    check("first page shows 48 games", cards(p) == 48, str(cards(p)))
    check("count line shows the whole catalog", "124 tracked games" in p.inner_text("#count"), p.inner_text("#count"))
    check("Load more is offered", p.locator("#more").count() == 1)
    p.click("#more")
    wait_cards(p, 96)
    check("Load more appends the next 48", cards(p) == 96)
    p.click("#more")
    wait_cards(p, 124)
    check("the last page is partial and the button goes away", cards(p) == 124 and p.locator("#more").count() == 0)

    p.fill("#q", "hades")
    wait_cards(p, 1)
    check("search resets to the first page of matches",
          "1 game matches" in p.inner_text("#count") and p.locator("#more").count() == 0, p.inner_text("#count"))

    # Load more racing with a new search must not mix pages.
    p.fill("#q", "")
    wait_cards(p, 48)
    p.evaluate("""() => { const f = window.fetch;
        window.fetch = (u, ...a) => String(u).includes('offset=48')
          ? new Promise(r => setTimeout(r, 900)).then(() => f(u, ...a)) : f(u, ...a); }""")
    p.click("#more")
    p.fill("#q", "hades")
    wait_cards(p, 1)
    p.wait_for_timeout(1300)  # long enough for the slow second page to arrive
    check("a slow Load more that lost the race is ignored", cards(p) == 1 and "1 game matches" in p.inner_text("#count"),
          f"{cards(p)} cards, {p.inner_text('#count')}")


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        check_list(page)
        browser.close()
    print(f"\n{len(failures)} failed" if failures else "\nall checks passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Start the demo app and run the check to see it fail**

From `price-match/` (PowerShell shown; use `export` in bash):

```powershell
$env:DATABASE_URL = "sqlite:///demo.db"
python -m scripts.seed_demo
$env:FRONTEND_DIR = "../frontend"
Start-Process python -ArgumentList "-m","uvicorn","app.main:app","--port","8765"
python -m scripts.browser_check http://localhost:8765/
```

Expected: `demo data ready`, then FAIL on "Load more is offered" (the list currently fetches 50 games and has no `#more`), and a non-zero exit code. Leave the server running for the next steps.

- [ ] **Step 5: Implement the paged list**

In `frontend/app.js` add two fields to `state`:

```js
const state = {view: null, seq: 0, searchSeq: 0, searching: false, q: "", games: null, total: 0, scrollY: 0,
               gameId: null, region: "US", regions: [], currency: "USD", ro: null};
```

Replace the whole section from the comment `/* ---- games list` up to (not including) the comment `/* ---- game profile` with:

```js
/* ---- games list --------------------------------------------------------- */

const PAGE = 48;

async function getPage(q, offset) {
  const r = await fetch(`${API}/games?limit=${PAGE}&offset=${offset}&q=${encodeURIComponent(q)}`);
  if (!r.ok) throw new Error(r.status);
  return {games: await r.json(), total: Number(r.headers.get("X-Total-Count")) || 0};
}

function showList() {
  document.title = "PlayMatch";
  if (state.ro) { state.ro.disconnect(); state.ro = null; }
  view.innerHTML = `<div class="wrap">
    <section class="intro">
      <h1>Find the lowest price for a PC game</h1>
      <p>Compare Steam, GOG and Epic side by side, and see whether today's price is a good one.</p>
      <input id="q" class="search" type="search" placeholder="Search a game, for example Hades" aria-label="Search games" autocomplete="off">
    </section>
    <p id="count" class="count muted" role="status"></p>
    <ul id="grid" class="grid"></ul>
    <div id="more-wrap" class="more-wrap"></div>
    <div id="notice"></div>
  </div>`;
  const q = $("#q");
  q.value = state.q;
  let timer;
  q.addEventListener("input", e => { clearTimeout(timer); timer = setTimeout(() => search(e.target.value.trim()), 200); });
  const cached = state.games !== null;
  if (cached) renderGrid();
  else search(state.q);
  window.scrollTo(0, cached ? state.scrollY : 0);
  if (!cached) focusHeading();
}

async function search(q) {
  state.q = q;
  const n = ++state.searchSeq;
  state.searching = true;
  try {
    const {games, total} = await getPage(q, 0);
    if (n !== state.searchSeq || state.view !== "list") return;
    state.games = games; state.total = total;
    renderGrid();
  } catch {
    if (n !== state.searchSeq || state.view !== "list") return;
    $("#grid").innerHTML = ""; $("#count").textContent = ""; $("#more-wrap").innerHTML = "";
    $("#notice").innerHTML = `<div class="notice"><strong>Can't reach the price service</strong>
      <p>Check that PlayMatch is running, then try again.</p><button class="btn" id="retry">Try again</button></div>`;
    $("#retry").onclick = () => search(state.q);
  } finally {
    if (n === state.searchSeq) state.searching = false;
  }
}

async function loadMore() {
  if (state.searching) return;
  const seq = state.searchSeq, shown = state.games.length, btn = $("#more");
  btn.disabled = true; btn.textContent = "Loading…";
  try {
    const {games, total} = await getPage(state.q, shown);
    if (seq !== state.searchSeq || state.view !== "list") return;   // a newer search replaced this list
    state.games = state.games.concat(games); state.total = total;
    renderGrid();
    const first = view.querySelectorAll("#grid .game")[shown];
    if (first) first.focus({preventScroll: true});
  } catch {
    if (seq === state.searchSeq && btn.isConnected) { btn.disabled = false; btn.textContent = "Couldn't load more. Try again"; }
  }
}

function renderGrid() {
  const games = state.games || [], total = state.total.toLocaleString();
  $("#notice").innerHTML = "";
  $("#count").textContent = state.q
    ? `${total} ${state.total === 1 ? "game matches" : "games match"} “${state.q}”`
    : `${total} tracked ${state.total === 1 ? "game" : "games"}`;
  $("#grid").innerHTML = games.map(g => `<li><a class="game" href="#/game/${g.id}">${art(g)}
      <span class="game-body"><span class="game-title">${esc(g.title)}</span><span class="game-year">${g.release_year ?? ""}</span></span></a></li>`).join("");
  $("#more-wrap").innerHTML = games.length < state.total
    ? `<button class="btn more" id="more">Load more</button><p class="muted">Showing ${games.length.toLocaleString()} of ${total}</p>` : "";
  const more = $("#more"); if (more) more.onclick = loadMore;
  if (!games.length) $("#notice").innerHTML = state.q
    ? `<div class="notice"><strong>No tracked game matches “${esc(state.q)}”</strong><p>Check the spelling, or try a shorter title.</p></div>`
    : `<div class="notice"><strong>No games are tracked yet</strong><p>Seed the list with <code>snapshot --seed</code>, or add a game through the admin API.</p></div>`;
}
```

In `frontend/app.css` change the `.grid` rule's `padding:0 0 72px` to `padding:0 0 32px` and add after it:

```css
.more-wrap{text-align:center;padding-bottom:72px}
.more-wrap:empty{padding-bottom:40px}
.more-wrap p{margin-top:10px}
.btn:disabled{opacity:.6;cursor:default}
```

- [ ] **Step 6: Run the browser check to verify it passes**

Run: `python -m scripts.browser_check http://localhost:8765/`
Expected: every line `PASS`, `all checks passed`, exit code 0. Hard-refresh is not needed; the script opens a fresh browser.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat: page the games list with Load more and add browser checks" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 5: Epic checking states and "last checked" on the profile

**Files:**
- Modify: `price-match/scripts/browser_check.py` (add `check_profile`)
- Modify: `frontend/app.js` (`showGame`, `renderProfile`, `pricesPanel`, helpers)
- Modify: `frontend/app.css`

**Interfaces:**
- Consumes: `POST /api/games/{id}/epic-check` (Task 3) returning `{status, checked_at}`; `p.checks` and `r.checked_at` from the prices response (Task 2); the demo games from Task 4.
- Produces: profile behavior: a placeholder Epic row ("Checking the Epic price…") while a lookup runs, replaced by the refreshed prices when it returns `checked` or `fresh`, or by an explanatory line on `busy` or `unavailable`; "Checked <date>" under each store price, and "Price may be out of date" when that is over 14 days ago.

- [ ] **Step 1: Add the failing profile checks**

In `price-match/scripts/browser_check.py`, add above `def main()`:

```python
def game_id(p, query):
    return p.evaluate("q => fetch('/api/games?q=' + encodeURIComponent(q)).then(r => r.json()).then(g => g[0].id)", query)


def open_game(p, gid):
    """Open a profile. Going through the list first guarantees the hash changes, so the router always runs."""
    p.goto(BASE + "#/")
    p.goto(BASE + f"#/game/{gid}")


def check_profile(p):
    posts = []
    p.on("request", lambda r: posts.append(r.url) if r.method == "POST" and "epic-check" in r.url else None)
    state = {"status": "unavailable"}

    def fulfill(route):
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"status": state["status"], "checked_at": None}))

    p.route("**/epic-check*", fulfill)
    fresh, hades, old, free = (game_id(p, n) for n in ("Fresh Steam Only", "Hades", "Old Price Game", "Free Game"))

    # A game whose Epic price was never checked: shows "checking", then the outcome.
    open_game(p, fresh)
    p.wait_for_selector("#epic-row")
    check("a placeholder Epic row says it is checking", "Checking" in p.inner_text("#epic-row"), p.inner_text("#epic-row"))
    p.wait_for_function("document.querySelector('#epic-row') && /unavailable/.test(document.querySelector('#epic-row').innerText)")
    check("an unavailable Epic price is explained", "Open this game again later" in p.inner_text("#epic-row"))
    check("exactly one Epic lookup was requested", len(posts) == 1, str(len(posts)))

    state["status"] = "busy"
    open_game(p, fresh)
    p.wait_for_function("document.querySelector('#epic-row') && /busy/.test(document.querySelector('#epic-row').innerText)")
    check("a busy Epic lookup says to try again in a minute", "in a minute" in p.inner_text("#epic-row"))

    # 'checked' re-renders with refreshed data and must not loop.
    state["status"] = "checked"
    before = len(posts)
    open_game(p, fresh)
    p.wait_for_selector("#epic-row")
    p.wait_for_function("!document.querySelector('#epic-row')")
    p.wait_for_timeout(1500)
    check("a checked lookup refreshes the page and does not ask again", len(posts) == before + 1, f"{len(posts) - before} requests")

    # A game whose Epic price was checked recently: no lookup at all.
    before = len(posts)
    open_game(p, hades)
    p.wait_for_selector(".prices")
    p.wait_for_timeout(1200)
    check("a recently checked game makes no Epic request", len(posts) == before and p.locator("#epic-row").count() == 0)

    # Stale Steam price.
    open_game(p, old)
    p.wait_for_selector(".prices")
    check("a price checked over 14 days ago says it may be out of date", "may be out of date" in p.inner_text(".prices"))
    open_game(p, hades)
    p.wait_for_selector(".prices")
    check("a recent price shows its check date and no warning",
          "Checked" in p.inner_text(".prices") and "out of date" not in p.inner_text(".prices"))

    # No prices at all and an Epic lookup pending: the prices section still appears.
    state["status"] = "unavailable"
    open_game(p, free)
    p.wait_for_selector("#epic-row")
    check("a game with no prices still shows the Epic row while checking", p.locator(".prices").count() == 1)

    # Regression: the back link keeps the search and scroll.
    p.goto(BASE + "#/")
    p.wait_for_selector("#grid .game")
    p.fill("#q", "")
    wait_cards(p, 48)   # start from a clean list so the check below is not trivially true
    p.fill("#q", "hades")
    wait_cards(p, 1)
    p.click("#grid .game")
    p.wait_for_selector(".back")
    p.click(".back")
    p.wait_for_selector("#grid .game")
    check("All games returns to the same search", p.input_value("#q") == "hades" and cards(p) == 1)
```

and change `main()` so the browser context delays epic-check responses (so the "checking" state is visible) and both checks run:

```python
def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.add_init_script("""(() => { const f = window.fetch;
            window.fetch = (u, ...a) => String(u).includes('epic-check')
              ? new Promise(r => setTimeout(r, 700)).then(() => f(u, ...a)) : f(u, ...a); })()""")
        check_list(page)
        check_profile(page)
        browser.close()
    print(f"\n{len(failures)} failed" if failures else "\nall checks passed")
    sys.exit(1 if failures else 0)
```

- [ ] **Step 2: Run to verify the new checks fail**

Run (the Task 4 server should still be running; restart it if not): `python -m scripts.browser_check http://localhost:8765/`
Expected: the list checks PASS; the first profile check FAILs (`#epic-row` never appears, so the script times out after 30 seconds with a Playwright timeout error) and the exit code is non-zero.

- [ ] **Step 3: Implement the profile behavior**

In `frontend/app.js`:

(a) Below the `get` helper add:

```js
const post = async p => { const r = await fetch(API + p, {method: "POST"}); if (!r.ok) throw new Error(r.status); return r.json(); };
const EPIC_STALE_MS = 24 * 3600e3, OLD_PRICE_MS = 14 * 24 * 3600e3;
```

(b) Replace the whole `showGame` function with:

```js
async function showGame(seq, keepScroll, quiet) {
  if (state.ro) { state.ro.disconnect(); state.ro = null; }
  if (!quiet) {
    if (!keepScroll) window.scrollTo(0, 0);
    view.innerHTML = `<div class="wrap">${backLink}<p class="loading" role="status">Loading prices…</p></div>`;
  }
  const id = state.gameId, qs = `currency=${state.currency}&region=${state.region}`;
  try {
    const [p, h] = await Promise.all([get(`/games/${id}/prices?${qs}`), get(`/games/${id}/history?${qs}&days=365`)]);
    if (seq !== state.seq) return;
    const lookup = !quiet && needsEpicCheck(p);
    document.title = `${p.game.title} – PlayMatch`;
    view.innerHTML = renderProfile(p, h, lookup ? "checking" : null);
    const model = buildModel(h, p);
    if (model) mountChart($("#chart"), model, h.currency, p.game.title);
    if (!quiet && !keepScroll) focusHeading();
    if (lookup) runEpicCheck(seq);
  } catch (e) {
    if (seq !== state.seq) return;
    const missing = e.message === "404";
    view.innerHTML = `<div class="wrap">${backLink}<div class="notice"><strong>${missing ? "PlayMatch doesn't track that game" : "Couldn't load prices for this game"}</strong>
      <p>${missing ? "It may have been removed. Go back to the list to pick another." : "The price service didn't respond. Try again in a moment."}</p>
      ${missing ? "" : `<button class="btn" id="retry">Try again</button>`}</div></div>`;
    const r = $("#retry"); if (r) r.onclick = () => showGame(++state.seq);
  }
}

// The Epic price is fetched when a game is opened and it was not checked in the last day.
function needsEpicCheck(p) {
  const t = p.checks && p.checks.epic;
  return !t || Date.now() - Date.parse(t) > EPIC_STALE_MS;
}

const EPIC_NOTE = {
  checking: "Checking the Epic price…",
  busy: "Epic lookups are busy. Open this game again in a minute.",
  unavailable: "Epic price unavailable right now. Open this game again later.",
};

async function runEpicCheck(seq) {
  let status = "unavailable";
  try { status = (await post(`/games/${state.gameId}/epic-check?region=${state.region}`)).status; } catch {}
  if (seq !== state.seq) return;
  if (status === "checked" || status === "fresh") { showGame(seq, true, true); return; }   // re-render with the new prices
  const cell = $("#epic-row td.muted");
  if (cell) { cell.removeAttribute("role"); cell.textContent = EPIC_NOTE[status] || EPIC_NOTE.unavailable; }
}
```

(c) Change the `renderProfile` signature to `function renderProfile(p, h, epic) {`, and replace the line in its returned template that begins `<div class="wrap pf-body">` with:

```js
    <div class="wrap pf-body">${p.prices.length || epic ? pricesPanel(p, epic) : ""}${historyPanel(h, p)}</div>`;
```

(d) Replace the whole `pricesPanel` function with:

```js
function pricesPanel(p, epic) {
  const rows = p.prices.map(r => {
    const old = r.checked_at && Date.now() - Date.parse(r.checked_at) > OLD_PRICE_MS;
    const checked = r.checked_at ? `<span class="conv muted ${old ? "stale" : ""}">${old ? "Price may be out of date. " : ""}Checked ${fmtDate(Date.parse(r.checked_at), true)}</span>` : "";
    return `<tr>
    <td><span class="store-name">${esc(r.store)}</span><span class="kind ${r.store_type === "marketplace" ? "mk" : ""}">${esc(r.label)}</span></td>
    <td>${ptag(r.price_cents, r.currency, r.is_lowest ? "is-low" : "")}${r.is_lowest ? `<span class="vh"> lowest</span>` : ""}
      ${r.discount_pct ? `<span class="was">${money(r.base_price_cents, r.currency)}</span><span class="off">−${r.discount_pct}%</span>` : ""}
      ${r.converted ? `<span class="conv muted">converted from ${money(r.native_price_cents, r.native_currency)}</span>` : ""}${checked}</td>
    <td class="go">${r.url ? `<a href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">View<span class="vh"> ${esc(p.game.title)} on ${esc(r.store)}</span></a>` : ""}</td></tr>`;
  }).join("");
  const hasEpic = p.prices.some(r => r.store_id === "epic");
  const epicRow = epic && !hasEpic
    ? `<tr id="epic-row"><td><span class="store-name">Epic Games</span></td><td colspan="2" class="muted" ${epic === "checking" ? 'role="status"' : ""}>${EPIC_NOTE[epic]}</td></tr>` : "";
  return `<section class="panel"><div class="panel-head"><h2>Prices by store</h2></div>
    <div class="scroll"><table class="prices"><thead><tr><th>Store</th><th>Price</th><th><span class="vh">Link</span></th></tr></thead><tbody>${rows}${epicRow}</tbody></table></div></section>`;
}
```

(e) In `frontend/app.js`, change the `reloadGame` line to keep working with the new signature (no change needed: `showGame(++state.seq, true)` still passes `keepScroll` and leaves `quiet` undefined). Confirm by reading the line.

In `frontend/app.css` add:

```css
.stale{color:var(--ink);font-weight:600}
```

- [ ] **Step 4: Run the browser checks to verify they pass**

Run: `python -m scripts.browser_check http://localhost:8765/`
Expected: every list and profile check prints `PASS`, `all checks passed`, exit code 0. If "a checked lookup refreshes the page and does not ask again" fails with more than one request, the quiet re-render is re-triggering the lookup: check that `showGame(seq, true, true)` is what `runEpicCheck` calls.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: fetch the Epic price when a game is opened and show when prices were last checked" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 6: Docs and final verification

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: everything in both plans.
- Produces: README documentation of the API changes, the Epic lookup limits, and the browser check.

- [ ] **Step 1: Update the README**

In `README.md`, add this section after the `## Frontend` section:

```markdown
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
```

In `## Known limits` add: `- Two people opening the same never-checked game at the same instant can trigger two Epic searches (the global cap still applies).`

- [ ] **Step 2: Run the whole backend suite**

Run (from `price-match/`): `python -m pytest -q`
Expected: every test passes (rerun once if the known flaky test is the only failure).

- [ ] **Step 3: Run the browser checks against a fresh demo database**

Stop any running demo server, delete `demo.db`, then repeat the Task 4 Step 4 commands and run `python -m scripts.browser_check http://localhost:8765/`.
Expected: `all checks passed`.

- [ ] **Step 4: Look at it**

With the demo server running, take screenshots with Playwright at 1280 and 390 pixel widths, in light and dark, of: the list after one "Load more"; the "Fresh Steam Only" profile while checking; the "Old Price Game" profile. Open each image and confirm: no horizontal scroll at 390; the "Load more" button and count line are readable; the checking row does not shift the layout when it resolves; the out-of-date note is legible in both themes. Fix any defect found in `app.css` and re-run Step 3.

- [ ] **Step 5: Verify the images build**

Run (from the project root): `docker compose build`
Expected: both images build.

- [ ] **Step 6: Clean up**

Stop the demo server, delete `price-match/demo.db` and any screenshots you saved inside the project.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "docs: document the paged list API, Epic on-demand lookup and browser checks" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Self-review (run against the spec)

- **API paging, `X-Total-Count`, CORS exposure, limits:** Task 1.
- **`checked_at` per store and `checks`:** Task 2 (gog falls back to its latest snapshot time).
- **Epic on demand:** Task 3 (cooldown, global cap, 15-minute block, no-match counts as checked, errors do not).
- **List UI (48 per page, Load more, whole-catalog count, search resets, race with search):** Task 4.
- **Profile UI (checking row, statuses, refresh without loop, 14-day out-of-date note, no-price game):** Task 5.
- **Docs, verification, visual check, Docker build:** Task 6.
- **Type and name consistency:** `Gate.allow/blocked/block`, `check_epic(s, game, region, connector, gate, now)`, `default_gate`, `needsEpicCheck`, `runEpicCheck(seq)`, `showGame(seq, keepScroll, quiet)`, `renderProfile(p, h, epic)`, `pricesPanel(p, epic)` and `EPIC_NOTE` keys (`checking`, `busy`, `unavailable`) are defined once and used identically. The `#more`, `#more-wrap` and `#epic-row` ids used by `browser_check.py` are exactly those the JavaScript renders.
- **Depends on Plan 1:** `PriceCheck`, `mark_checked`, `as_utc`, `record_snapshot_if_due`, `config.EPIC_*`, `db_session`, `scale_test.populate`.
