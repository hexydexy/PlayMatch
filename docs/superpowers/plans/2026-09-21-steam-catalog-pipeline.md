# Steam Catalog Pipeline Implementation Plan (Plan 1 of 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The daily job lists every Steam base game, prices all of them through batched requests with change-only snapshots, and imports GOG prices for them at scale.

**Architecture:** The daily job in `snapshot.py` becomes a pipeline of bulk steps: catalog sync (new `catalog.py`), FX, bulk Steam prices (new `bulk.py`, resumable via a new `price_checks` table), Epic for seed games only, then the existing GOG dump import with a review-queue cap. No existing table is altered.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, httpx, rapidfuzz, pytest (SQLite in tests, Postgres in Docker).

**Spec:** `docs/superpowers/specs/2026-09-21-steam-catalog-design.md`. Plan 2 (`2026-09-21-epic-on-demand-and-ui.md`) covers the API paging, Epic on demand and the UI, and depends on the interfaces this plan produces.

## Global Constraints

- US region only. Region-keyed code paths stay as they are.
- New table only: `price_checks`, created by `create_all`. No column is added to any existing table (the project has no migration tool).
- Snapshots are stored only when a price changes, plus a heartbeat every `SNAPSHOT_HEARTBEAT_DAYS` (default 7).
- `STEAM_BATCH_SIZE` default 50; the probe tests 50 and 100. Outbound requests stay spaced by `REQUEST_DELAY`.
- Catalog sync lists base games only: `include_games=true`, `include_dlc=false`, `include_software=false`, `include_videos=false`, `include_hardware=false`, `max_results=50000`. It needs `STEAM_API_KEY`; without it the step is skipped and the job continues.
- Free-to-play and unreleased games are listed but get no price rows (Steam returns no `price_overview`).
- `REVIEW_QUEUE_CAP_PER_RUN` default 200: bulk GOG runs queue at most that many near-matches, highest scores first.
- The daily job refreshes Epic only for seed games: the entries in `price-match/app/seed_games.json`, identified by `steam_app_id`.
- Run tests from `price-match/` with `python -m pytest -q`. `test_history_stats_and_historical_low` is a known flaky test (identical timestamps on Windows); if it is the only failure, rerun it. It is not caused by this work.
- The project is not currently a git repo. Task 1 Step 1 initializes one so later commit steps and reviews have a diff. If the user declines `git init`, skip every commit step.
- Every commit message ends with the trailer `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>` (second `-m` argument in the commands below).

## Review Focus

Failure modes the spec implies that no single task's happy path exercises. Each has a test in the task named in brackets.

1. Two different Steam apps share a title (for example "Prey", 2006 and 2017). The catalog must keep both as separate games and never merge them by title. [Task 3]
2. Steam returns `data: []`, `success: false`, or omits an id entirely for free, unreleased or delisted games. None may raise, none may create a price row, and all must still be marked checked so they are not retried forever. [Tasks 4, 5]
3. Catalog games have no release year, so the matcher's fuzzy auto-match (which needs both years) never fires for them. Only exact normalized-title matches auto-link to GOG; near-matches go to the capped review queue. This is a consequence of the design, and it is pinned by a test so a future change to it is deliberate. [Task 7]
4. SQLite returns naive datetimes and Postgres returns aware ones. The heartbeat comparison must work with both. [Task 2]
5. A run cut short by a Steam rate limit must resume with the never-checked games first, and a bad or missing API key must not stop pricing. [Tasks 3, 5, 6]

---

### Task 1: Foundations: git baseline, config, `price_checks`, test fixtures

**Files:**
- Create: `price-match/tests/conftest.py`
- Create: `price-match/tests/test_foundations.py`
- Modify: `price-match/app/config.py`
- Modify: `price-match/app/models.py` (add `PriceCheck`)
- Modify: `price-match/app/service.py` (imports; add `as_utc`, `mark_checked`)
- Modify: `.env.example`, `docker-compose.yml`

**Interfaces:**
- Consumes: `app.db.Base`, `app.models.Game`, existing `service.upsert_game`.
- Produces: `config.STEAM_API_KEY: str`, `config.STEAM_BATCH_SIZE: int`, `config.SNAPSHOT_HEARTBEAT_DAYS: int`, `config.EPIC_ON_DEMAND_PER_MINUTE: int`, `config.EPIC_COOLDOWN_HOURS: int`, `config.REVIEW_QUEUE_CAP_PER_RUN: int`; `models.PriceCheck(game_id, store_id, region, checked_at)`; `service.as_utc(dt: datetime) -> datetime`; `service.mark_checked(s: Session, game_ids: list[int], store_id: str, region: str, now: datetime | None = None) -> None`; pytest fixture `db_session` (a fresh in-memory SQLite session with stores seeded) and an autouse fixture that sets `config.REQUEST_DELAY = 0`.

- [ ] **Step 1: Initialize git and commit the baseline**

From `C:\Users\vetsa\playmatch`:

```bash
git init
git add -A
git commit -m "chore: baseline PlayMatch v1_2" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

Expected: a root commit. Check that `git status` is clean and that `.gitignore` keeps `__pycache__`, `.pytest_cache` and `*.db` out (if it does not, add those three lines to `.gitignore` and amend the baseline commit).

- [ ] **Step 2: Write the shared fixtures**

Create `price-match/tests/conftest.py`:

```python
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import config, db, service


@pytest.fixture(autouse=True)
def _no_request_delay(monkeypatch):
    monkeypatch.setattr(config, "REQUEST_DELAY", 0)


@pytest.fixture()
def db_session():
    """A fresh in-memory database with the three stores seeded."""
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from app import models  # noqa: F401  (registers tables)
    db.Base.metadata.create_all(eng)
    with sessionmaker(bind=eng, expire_on_commit=False)() as sess:
        service.seed_stores(sess)
        yield sess
```

- [ ] **Step 3: Write the failing tests**

Create `price-match/tests/test_foundations.py`:

```python
from datetime import datetime, timedelta, timezone

from app import service
from app.models import PriceCheck

T1 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def test_mark_checked_creates_then_updates(db_session):
    g1 = service.upsert_game(db_session, "Hades", 1145360, 2020)
    g2 = service.upsert_game(db_session, "Celeste", 504230, 2018)
    service.mark_checked(db_session, [g1.id, g2.id], "steam", "US", T1)
    service.mark_checked(db_session, [g1.id], "steam", "US", T1 + timedelta(days=1))
    rows = {r.game_id: service.as_utc(r.checked_at) for r in db_session.query(PriceCheck)}
    assert rows == {g1.id: T1 + timedelta(days=1), g2.id: T1}
    assert db_session.query(PriceCheck).count() == 2


def test_mark_checked_is_keyed_by_store_and_region(db_session):
    g = service.upsert_game(db_session, "Hades", 1145360, 2020)
    service.mark_checked(db_session, [g.id], "steam", "US", T1)
    service.mark_checked(db_session, [g.id], "epic", "US", T1)
    service.mark_checked(db_session, [g.id], "steam", "GB", T1)
    assert db_session.query(PriceCheck).count() == 3


def test_mark_checked_with_no_games_is_a_noop(db_session):
    service.mark_checked(db_session, [], "steam", "US", T1)
    assert db_session.query(PriceCheck).count() == 0


def test_as_utc_handles_naive_and_aware():
    naive = datetime(2026, 9, 1, 12, 0)
    assert service.as_utc(naive) == datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    aware = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    assert service.as_utc(aware) is aware
```

- [ ] **Step 4: Run to verify failure**

Run: `python -m pytest tests/test_foundations.py -q`
Expected: FAIL (`ImportError: cannot import name 'PriceCheck'`).

- [ ] **Step 5: Add the settings**

Append to `price-match/app/config.py`:

```python

# --- full Steam catalog -----------------------------------------------------
# Free Steam Web API key; needed to list every Steam game. Without it catalog sync is skipped.
STEAM_API_KEY = os.getenv("STEAM_API_KEY", "")
# Steam games priced per request (`python -m app.bulk --probe` finds the largest that works)
STEAM_BATCH_SIZE = int(os.getenv("STEAM_BATCH_SIZE", "50"))
# Store a snapshot only when the price changed, or when the last one is this many days old
SNAPSHOT_HEARTBEAT_DAYS = int(os.getenv("SNAPSHOT_HEARTBEAT_DAYS", "7"))
# Epic lookups triggered by opening a game
EPIC_ON_DEMAND_PER_MINUTE = int(os.getenv("EPIC_ON_DEMAND_PER_MINUTE", "30"))
EPIC_COOLDOWN_HOURS = int(os.getenv("EPIC_COOLDOWN_HOURS", "24"))
# Most GOG near-matches queued for manual review in one daily run
REVIEW_QUEUE_CAP_PER_RUN = int(os.getenv("REVIEW_QUEUE_CAP_PER_RUN", "200"))
```

- [ ] **Step 6: Add the model**

Append to `price-match/app/models.py`:

```python


class PriceCheck(Base):
    """When a store was last asked about a game.

    A snapshot's timestamp is only the last price *change* (unchanged prices are
    not re-stored), so "last checked" needs its own record. It also orders the bulk
    Steam refresh (never-checked games first) and drives the Epic on-demand cooldown.
    """
    __tablename__ = "price_checks"
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"), primary_key=True)
    store_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    region: Mapped[str] = mapped_column(String(8), primary_key=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
```

- [ ] **Step 7: Add the helpers to `service.py`**

In `price-match/app/service.py` change the imports:

```python
from datetime import datetime, timedelta, timezone
```

```python
from . import config, fx, regions
```

```python
from .models import (Game, JobRun, Listing, MatchCandidate, PriceCheck, PriceSnapshot,
                     Store, utcnow)
```

Add after `log_job`:

```python
def as_utc(dt: datetime) -> datetime:
    """SQLite hands back naive datetimes and Postgres aware ones; treat naive as UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def mark_checked(s: Session, game_ids: list[int], store_id: str, region: str,
                 now: datetime | None = None) -> None:
    """Record that `store_id` was asked about these games just now (insert or update)."""
    if not game_ids:
        return
    now = now or utcnow()
    have = {c.game_id: c for c in s.scalars(select(PriceCheck).where(
        PriceCheck.game_id.in_(game_ids), PriceCheck.store_id == store_id,
        PriceCheck.region == region))}
    for gid in game_ids:
        if gid in have:
            have[gid].checked_at = now
        else:
            s.add(PriceCheck(game_id=gid, store_id=store_id, region=region, checked_at=now))
    s.commit()
```

- [ ] **Step 8: Run to verify pass**

Run: `python -m pytest tests/test_foundations.py -q` then `python -m pytest -q`
Expected: the new file passes (4 tests); the full suite passes except possibly the known flaky test.

- [ ] **Step 9: Document the settings and pass them through Docker**

Append to `.env.example`:

```
# Free Steam Web API key (https://steamcommunity.com/dev/apikey). Needed to list every Steam game; without it the catalog is left as it is.
STEAM_API_KEY=
# Steam games priced per request. Start at 50; `python -m app.bulk --probe` finds the largest that works.
STEAM_BATCH_SIZE=50
# Store a price snapshot only when the price changed, or at least this often (days)
SNAPSHOT_HEARTBEAT_DAYS=7
# Epic lookups triggered by opening a game: per-minute cap and per-game cooldown (hours)
EPIC_ON_DEMAND_PER_MINUTE=30
EPIC_COOLDOWN_HOURS=24
# Most GOG near-matches queued for manual review per daily run
REVIEW_QUEUE_CAP_PER_RUN=200
```

In `docker-compose.yml`, directly under the line `      SCHEDULE_HOUR_UTC: ${SCHEDULE_HOUR_UTC:-6}` add:

```yaml
      STEAM_API_KEY: ${STEAM_API_KEY:-}
      STEAM_BATCH_SIZE: ${STEAM_BATCH_SIZE:-50}
      SNAPSHOT_HEARTBEAT_DAYS: ${SNAPSHOT_HEARTBEAT_DAYS:-7}
      EPIC_ON_DEMAND_PER_MINUTE: ${EPIC_ON_DEMAND_PER_MINUTE:-30}
      EPIC_COOLDOWN_HOURS: ${EPIC_COOLDOWN_HOURS:-24}
      REVIEW_QUEUE_CAP_PER_RUN: ${REVIEW_QUEUE_CAP_PER_RUN:-200}
```

Run: `docker compose config -q` (from `C:\Users\vetsa\playmatch`). Expected: no output and exit code 0.

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "feat: add price_checks table, catalog settings and shared test fixtures" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 2: Change-only snapshots

**Files:**
- Create: `price-match/tests/test_snapshots_due.py`
- Modify: `price-match/app/service.py` (add `latest_snapshot`, `record_snapshot_if_due`)

**Interfaces:**
- Consumes: `service.get_or_create_listing`, `service.as_utc`, `config.SNAPSHOT_HEARTBEAT_DAYS`, `models.PriceSnapshot`.
- Produces: `service.record_snapshot_if_due(s: Session, game: Game, raw: RawListing, now: datetime | None = None) -> PriceSnapshot | None`. It returns the new snapshot, or `None` when skipped. It flushes but does not commit; the caller commits.

- [ ] **Step 1: Write the failing tests**

Create `price-match/tests/test_snapshots_due.py`:

```python
from datetime import datetime, timedelta, timezone

from app import config, service
from app.connectors.base import RawListing
from app.models import PriceSnapshot

NOW = datetime(2026, 9, 21, 6, 0, tzinfo=timezone.utc)


def listing(price=1999, base=1999, currency="USD"):
    return RawListing("steam", "1145360", "Hades", "http://x", price, base, currency, steam_app_id=1145360)


def count(s):
    return s.query(PriceSnapshot).count()


def test_first_sighting_is_stored(db_session):
    g = service.upsert_game(db_session, "Hades", 1145360, 2020)
    snap = service.record_snapshot_if_due(db_session, g, listing(), NOW)
    assert snap is not None and count(db_session) == 1


def test_unchanged_price_inside_heartbeat_is_skipped(db_session):
    g = service.upsert_game(db_session, "Hades", 1145360, 2020)
    service.record_snapshot_if_due(db_session, g, listing(), NOW)
    assert service.record_snapshot_if_due(db_session, g, listing(), NOW + timedelta(days=1)) is None
    assert count(db_session) == 1


def test_unchanged_price_after_heartbeat_is_stored(db_session):
    g = service.upsert_game(db_session, "Hades", 1145360, 2020)
    service.record_snapshot_if_due(db_session, g, listing(), NOW)
    later = NOW + timedelta(days=config.SNAPSHOT_HEARTBEAT_DAYS, seconds=1)
    assert service.record_snapshot_if_due(db_session, g, listing(), later) is not None
    assert count(db_session) == 2


def test_changed_price_is_stored_immediately(db_session):
    g = service.upsert_game(db_session, "Hades", 1145360, 2020)
    service.record_snapshot_if_due(db_session, g, listing(1999), NOW)
    assert service.record_snapshot_if_due(db_session, g, listing(999), NOW + timedelta(hours=1)) is not None
    assert count(db_session) == 2


def test_changed_base_price_alone_is_stored(db_session):
    g = service.upsert_game(db_session, "Hades", 1145360, 2020)
    service.record_snapshot_if_due(db_session, g, listing(999, 1999), NOW)
    assert service.record_snapshot_if_due(db_session, g, listing(999, 2499), NOW + timedelta(hours=1)) is not None


def test_heartbeat_compares_correctly_when_the_database_returns_naive_datetimes(db_session):
    # SQLite returns naive datetimes even for tz-aware columns; Postgres returns aware ones.
    g = service.upsert_game(db_session, "Hades", 1145360, 2020)
    service.record_snapshot_if_due(db_session, g, listing(), NOW)
    db_session.commit()
    db_session.expire_all()
    stored = db_session.query(PriceSnapshot).one().captured_at
    assert stored.tzinfo is None  # confirms this test really exercises the naive path on SQLite
    assert service.record_snapshot_if_due(db_session, g, listing(), NOW + timedelta(days=2)) is None


def test_defaults_now_to_the_current_time(db_session):
    g = service.upsert_game(db_session, "Hades", 1145360, 2020)
    snap = service.record_snapshot_if_due(db_session, g, listing())
    assert abs(service.as_utc(snap.captured_at) - datetime.now(timezone.utc)) < timedelta(seconds=30)
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_snapshots_due.py -q`
Expected: FAIL (`AttributeError: module 'app.service' has no attribute 'record_snapshot_if_due'`).

- [ ] **Step 3: Implement**

In `price-match/app/service.py`, add after `record_snapshot`:

```python
def latest_snapshot(s: Session, listing_id: int) -> PriceSnapshot | None:
    return s.scalar(select(PriceSnapshot).where(PriceSnapshot.listing_id == listing_id)
                    .order_by(PriceSnapshot.captured_at.desc(), PriceSnapshot.id.desc()).limit(1))


def record_snapshot_if_due(s: Session, game: Game, raw: RawListing,
                           now: datetime | None = None) -> PriceSnapshot | None:
    """Like `record_snapshot`, but skips a price that has not changed.

    A snapshot is stored when the price, base price or currency differs from the
    latest one, or when the latest one is at least SNAPSHOT_HEARTBEAT_DAYS old (so the
    history keeps regular sample dates). Flushes; the caller commits.
    """
    now = now or utcnow()
    listing = get_or_create_listing(s, game, raw.store_id, raw.product_id, raw.region, raw.url)
    last = latest_snapshot(s, listing.id)
    if last is not None:
        same = (last.price_cents, last.base_price_cents, last.currency) == \
               (raw.price_cents, raw.base_price_cents, raw.currency)
        if same and now - as_utc(last.captured_at) < timedelta(days=config.SNAPSHOT_HEARTBEAT_DAYS):
            s.flush()
            return None
    snap = PriceSnapshot(listing_id=listing.id, price_cents=raw.price_cents,
                         base_price_cents=raw.base_price_cents, currency=raw.currency,
                         discount_pct=raw.discount_pct, captured_at=now)
    s.add(snap)
    s.flush()
    return snap
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_snapshots_due.py -q`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: store a price snapshot only when it changed or the heartbeat is due" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 3: Catalog sync

**Files:**
- Create: `price-match/app/catalog.py`
- Create: `price-match/tests/test_catalog.py`

**Interfaces:**
- Consumes: `config.STEAM_API_KEY`, `connectors.base.client/check_status/polite_sleep/ConnectorError`, `service.log_job`, `matcher.normalize`, `models.Game`.
- Produces: `catalog.parse_page(body: dict) -> tuple[list[tuple[int, str]], bool, int | None]`; `catalog.fetch_all(key: str) -> list[tuple[int, str]]`; `catalog.sync(s: Session, key: str | None = None, fetch=None) -> dict` returning `{"added", "renamed", "total"}` or `{"skipped": reason}`. `sync` never raises.

- [ ] **Step 1: Write the failing tests**

Create `price-match/tests/test_catalog.py`:

```python
import httpx
import pytest

from app import catalog, service
from app.models import Game, JobRun


def test_parse_page_skips_empty_names_and_returns_the_cursor():
    body = {"response": {
        "apps": [{"appid": 10, "name": "Counter-Strike"}, {"appid": 20, "name": "  "},
                 {"appid": 30, "name": " Half-Life "}],
        "have_more_results": True, "last_appid": 30}}
    apps, more, last = catalog.parse_page(body)
    assert apps == [(10, "Counter-Strike"), (30, "Half-Life")]
    assert more is True and last == 30


def test_parse_page_tolerates_missing_fields():
    assert catalog.parse_page({}) == ([], False, None)
    assert catalog.parse_page({"response": {}}) == ([], False, None)


def test_sync_inserts_new_games_without_a_release_year(db_session):
    rep = catalog.sync(db_session, key="k", fetch=lambda key: [(10, "Counter-Strike"), (30, "Half-Life")])
    assert rep == {"added": 2, "renamed": 0, "total": 2}
    g = db_session.query(Game).filter_by(steam_app_id=10).one()
    assert g.title == "Counter-Strike" and g.norm_title == "counter strike" and g.release_year is None


def test_sync_renames_but_keeps_other_data(db_session):
    service.upsert_game(db_session, "Hades", 1145360, 2020)
    rep = catalog.sync(db_session, key="k", fetch=lambda key: [(1145360, "Hades: Reborn")])
    assert rep == {"added": 0, "renamed": 1, "total": 1}
    g = db_session.query(Game).filter_by(steam_app_id=1145360).one()
    assert g.title == "Hades: Reborn" and g.norm_title == "hades reborn" and g.release_year == 2020


def test_sync_is_idempotent(db_session):
    apps = [(1, "A"), (2, "B")]
    catalog.sync(db_session, key="k", fetch=lambda key: apps)
    rep = catalog.sync(db_session, key="k", fetch=lambda key: apps)
    assert rep == {"added": 0, "renamed": 0, "total": 2}
    assert db_session.query(Game).count() == 2


def test_two_steam_apps_with_the_same_title_stay_separate_games(db_session):
    catalog.sync(db_session, key="k", fetch=lambda key: [(7110, "Prey"), (480490, "Prey")])
    assert db_session.query(Game).filter_by(title="Prey").count() == 2


def test_sync_does_not_merge_into_an_existing_title_only_game(db_session):
    # An Epic-only game added by title has no Steam id; a Steam app with the same title is still its own game.
    service.upsert_game(db_session, "Celeste")
    catalog.sync(db_session, key="k", fetch=lambda key: [(504230, "Celeste")])
    assert db_session.query(Game).count() == 2


def test_sync_without_a_key_skips_and_does_not_fetch(db_session, monkeypatch):
    monkeypatch.setattr(catalog.config, "STEAM_API_KEY", "")
    called = []
    rep = catalog.sync(db_session, fetch=lambda key: called.append(key) or [])
    assert rep == {"skipped": "no STEAM_API_KEY"} and not called
    assert db_session.query(Game).count() == 0


def test_fetch_error_is_logged_not_raised(db_session):
    def boom(key):
        raise catalog.ConnectorError("steam-catalog HTTP 403")
    rep = catalog.sync(db_session, key="bad", fetch=boom)
    assert rep["skipped"].startswith("error")
    assert db_session.query(JobRun).filter_by(kind="catalog_sync", outcome="error").count() == 1
    assert db_session.query(Game).count() == 0


def _mock(handler):
    return lambda: httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_all_follows_the_cursor_and_sends_the_filters(monkeypatch):
    calls = []

    def handler(req):
        calls.append(dict(req.url.params))
        if req.url.params["last_appid"] == "0":
            return httpx.Response(200, json={"response": {
                "apps": [{"appid": 1, "name": "A"}], "have_more_results": True, "last_appid": 1}})
        return httpx.Response(200, json={"response": {
            "apps": [{"appid": 2, "name": "B"}], "have_more_results": False}})

    monkeypatch.setattr(catalog, "client", _mock(handler))
    assert catalog.fetch_all("KEY") == [(1, "A"), (2, "B")]
    assert [c["last_appid"] for c in calls] == ["0", "1"]
    first = calls[0]
    assert first["key"] == "KEY" and first["include_games"] == "true" and first["include_dlc"] == "false"
    assert first["include_software"] == "false" and first["max_results"] == "50000"


def test_fetch_all_stops_when_steam_omits_the_cursor(monkeypatch):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(200, json={"response": {
            "apps": [{"appid": 1, "name": "A"}], "have_more_results": True}})

    monkeypatch.setattr(catalog, "client", _mock(handler))
    assert catalog.fetch_all("KEY") == [(1, "A")]
    assert len(calls) == 1  # would loop forever without the guard


def test_fetch_all_raises_on_a_rejected_key(monkeypatch):
    monkeypatch.setattr(catalog, "client", _mock(lambda req: httpx.Response(403, text="Forbidden")))
    with pytest.raises(catalog.ConnectorError):
        catalog.fetch_all("BAD")
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_catalog.py -q`
Expected: FAIL (`ImportError: cannot import name 'catalog'`).

- [ ] **Step 3: Implement**

Create `price-match/app/catalog.py`:

```python
"""Sync the list of Steam base games into `games`.

Uses IStoreService/GetAppList, which needs a free Steam Web API key (STEAM_API_KEY).
Games are keyed by Steam app id only: two apps with the same title stay two games.
The response shape below follows Steam's documentation; confirm it against a real
response the first time a key is used (see README, "First run with a key").
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config, service
from .connectors.base import ConnectorError, check_status, client, polite_sleep
from .matcher import normalize
from .models import Game

log = logging.getLogger("playmatch.catalog")

URL = "https://api.steampowered.com/IStoreService/GetAppList/v1/"
PAGE_SIZE = 50000
COMMIT_EVERY = 5000


def parse_page(body: dict) -> tuple[list[tuple[int, str]], bool, int | None]:
    """(apps as (app_id, name), have_more_results, last_appid) from one response page."""
    r = body.get("response") or {}
    apps = [(int(a["appid"]), a["name"].strip()) for a in r.get("apps", [])
            if (a.get("name") or "").strip()]
    return apps, bool(r.get("have_more_results")), r.get("last_appid")


def fetch_all(key: str) -> list[tuple[int, str]]:
    apps: list[tuple[int, str]] = []
    last = 0
    with client() as c:
        while True:
            r = c.get(URL, params={
                "key": key, "include_games": "true", "include_dlc": "false",
                "include_software": "false", "include_videos": "false",
                "include_hardware": "false", "max_results": PAGE_SIZE, "last_appid": last})
            polite_sleep()
            check_status(r, "steam-catalog", (429,))
            page, more, last_id = parse_page(r.json())
            apps.extend(page)
            if not more or not last_id:  # also stops if Steam ever omits the cursor
                break
            last = last_id
    return apps


def sync(s: Session, key: str | None = None, fetch=None) -> dict:
    """Insert new Steam games and refresh renamed ones. Never raises."""
    key = config.STEAM_API_KEY if key is None else key
    if not key:
        log.warning("STEAM_API_KEY is not set; skipping catalog sync")
        return {"skipped": "no STEAM_API_KEY"}
    try:
        apps = (fetch or fetch_all)(key)
    except Exception as e:
        log.error("catalog sync failed: %s", e)
        service.log_job(s, "catalog_sync", "steam", "-", "error", str(e))
        return {"skipped": f"error: {e}"}

    existing = {g.steam_app_id: g for g in s.scalars(select(Game).where(Game.steam_app_id.is_not(None)))}
    added = renamed = pending = 0
    for app_id, name in apps:
        g = existing.get(app_id)
        if g is None:
            g = Game(title=name, norm_title=normalize(name), steam_app_id=app_id)
            s.add(g)
            existing[app_id] = g
            added += 1
        elif g.title != name:
            g.title, g.norm_title = name, normalize(name)
            renamed += 1
        else:
            continue
        pending += 1
        if pending >= COMMIT_EVERY:
            s.commit()
            pending = 0
    s.commit()
    service.log_job(s, "catalog_sync", "steam", "-", "ok", f"{added} added, {renamed} renamed")
    return {"added": added, "renamed": renamed, "total": len(apps)}
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_catalog.py -q`
Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: sync the Steam base-game catalog into games" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 4: Batched Steam price lookups

**Files:**
- Modify: `price-match/app/connectors/steam.py`
- Create: `price-match/tests/test_steam_batch.py`

**Interfaces:**
- Consumes: `connectors.base.client/check_status/polite_sleep/ConnectorError/RateLimited/RawListing`, `regions.get`.
- Produces: `steam.parse_price_batch(payload: dict, app_ids: list[int], region: str = "US") -> dict[int, RawListing | None]` (every requested id is a key; `title` is `""`, the caller fills it); `SteamConnector.fetch_batch_payload(app_ids: list[int], region: str = "US") -> dict` (the raw JSON object); `SteamConnector.prices_for(app_ids: list[int], region: str = "US") -> dict[int, RawListing | None]`. Raises `RateLimited` on HTTP 429/403 and `ConnectorError` on any other failure or a non-object body.

- [ ] **Step 1: Write the failing tests**

Create `price-match/tests/test_steam_batch.py`:

```python
import httpx
import pytest

from app.connectors import steam
from app.connectors.base import ConnectorError, RateLimited

PRICED = {"success": True, "data": {"price_overview": {
    "currency": "USD", "initial": 5999, "final": 2999, "discount_percent": 50}}}


def _mock(handler):
    return lambda: httpx.Client(transport=httpx.MockTransport(handler))


def test_parse_price_batch_handles_every_shape():
    payload = {"1": PRICED, "2": {"success": True, "data": []}, "3": {"success": False}}  # id 4 omitted
    out = steam.parse_price_batch(payload, [1, 2, 3, 4])
    assert set(out) == {1, 2, 3, 4}
    one = out[1]
    assert (one.price_cents, one.base_price_cents, one.currency) == (2999, 5999, "USD")
    assert one.product_id == "1" and one.steam_app_id == 1 and one.url.endswith("/app/1")
    assert out[2] is None and out[3] is None and out[4] is None


def test_prices_for_sends_one_request_for_all_ids(monkeypatch):
    seen = []

    def handler(req):
        seen.append(dict(req.url.params))
        return httpx.Response(200, json={"1": PRICED, "2": PRICED})

    monkeypatch.setattr(steam, "client", _mock(handler))
    out = steam.SteamConnector().prices_for([1, 2], "US")
    assert len(seen) == 1
    assert seen[0]["appids"] == "1,2" and seen[0]["filters"] == "price_overview" and seen[0]["cc"] == "US"
    assert out[1].price_cents == 2999 and out[2].price_cents == 2999


@pytest.mark.parametrize("status", [429, 403])
def test_prices_for_raises_rate_limited(monkeypatch, status):
    monkeypatch.setattr(steam, "client", _mock(lambda req: httpx.Response(status)))
    with pytest.raises(RateLimited):
        steam.SteamConnector().prices_for([1], "US")


def test_prices_for_treats_a_non_object_body_as_an_error(monkeypatch):
    monkeypatch.setattr(steam, "client", _mock(lambda req: httpx.Response(200, json=[])))
    with pytest.raises(ConnectorError):
        steam.SteamConnector().prices_for([1, 2], "US")


def test_prices_for_treats_a_server_error_as_an_error(monkeypatch):
    monkeypatch.setattr(steam, "client", _mock(lambda req: httpx.Response(500)))
    with pytest.raises(ConnectorError):
        steam.SteamConnector().prices_for([1], "US")
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_steam_batch.py -q`
Expected: FAIL (`AttributeError: module 'app.connectors.steam' has no attribute 'parse_price_batch'`).

- [ ] **Step 3: Implement**

In `price-match/app/connectors/steam.py` change the base import to:

```python
from .base import ConnectorError, RawListing, check_status, client, polite_sleep
```

Add after `parse_details`:

```python
def parse_price_batch(payload: dict, app_ids: list[int], region: str = "US") -> dict[int, RawListing | None]:
    """Prices from a multi-id `price_overview` response. Every requested id is a key;
    free, unreleased, delisted or omitted ids map to None."""
    out: dict[int, RawListing | None] = {}
    for app_id in app_ids:
        entry = payload.get(str(app_id)) or {}
        po = (entry.get("data") or {}).get("price_overview") if entry.get("success") else None
        out[app_id] = None if not po else RawListing(
            store_id="steam", product_id=str(app_id), title="",
            url=f"https://store.steampowered.com/app/{app_id}",
            price_cents=int(po["final"]), base_price_cents=int(po["initial"]),
            currency=po["currency"], steam_app_id=app_id, region=region)
    return out
```

Add to `SteamConnector`:

```python
    def fetch_batch_payload(self, app_ids: list[int], region: str = "US") -> dict:
        """One request for many ids; returns Steam's raw JSON object."""
        with client() as c:
            r = c.get(DETAILS, params={"appids": ",".join(str(a) for a in app_ids),
                                       "cc": regions.get(region).code, "filters": "price_overview"})
            polite_sleep()
            check_status(r, "steam", LIMITED)
            payload = r.json()
        if not isinstance(payload, dict):
            raise ConnectorError("steam batch: unexpected response body")
        return payload

    def prices_for(self, app_ids: list[int], region: str = "US") -> dict[int, RawListing | None]:
        return parse_price_batch(self.fetch_batch_payload(app_ids, region), app_ids, region)
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_steam_batch.py -q`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: price many Steam games per request" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 5: Bulk Steam refresh (resumable) and the batch-size probe

**Files:**
- Create: `price-match/app/bulk.py`
- Create: `price-match/tests/test_bulk.py`

**Interfaces:**
- Consumes: `service.record_snapshot_if_due`, `service.mark_checked`, `service.log_job`, `config.STEAM_BATCH_SIZE`, `SteamConnector.prices_for` / `fetch_batch_payload`, `models.Game/PriceCheck`, `RateLimited`.
- Produces: `bulk.games_due(s: Session, region: str) -> list[tuple[int, int, str]]` (game id, Steam app id, title; never-checked first, then oldest check); `bulk.refresh_steam_prices(s: Session, region: str = "US", connector=None, batch_size: int | None = None, blocked: set[str] | None = None) -> dict` with keys `games, batches, priced, no_price, snapshots, errors, rate_limited`; `bulk.probe_batch_size(connector, app_ids: list[int], region: str = "US", sizes=(50, 100)) -> int`; command `python -m app.bulk --probe`.

- [ ] **Step 1: Write the failing tests**

Create `price-match/tests/test_bulk.py`:

```python
from datetime import datetime, timedelta, timezone

from app import bulk, service
from app.connectors.base import ConnectorError, RateLimited, RawListing
from app.models import JobRun, PriceCheck, PriceSnapshot

T = datetime(2026, 9, 1, tzinfo=timezone.utc)


def priced(app_id, price=1999):
    return RawListing("steam", str(app_id), "", f"https://store.steampowered.com/app/{app_id}",
                      price, 2999, "USD", steam_app_id=app_id)


class FakeSteam:
    store_id = "steam"

    def __init__(self, prices=None, fail=None):
        self.prices, self.fail, self.calls = prices or {}, fail, []

    def prices_for(self, app_ids, region="US"):
        self.calls.append(list(app_ids))
        if self.fail:
            self.fail(app_ids, len(self.calls))
        return {a: self.prices.get(a) for a in app_ids}


def add_games(s, n):
    return [service.upsert_game(s, f"Game {i}", 1000 + i) for i in range(n)]


def test_games_due_lists_unchecked_first_then_oldest_and_skips_games_without_a_steam_id(db_session):
    g1, g2, g3 = add_games(db_session, 3)
    service.upsert_game(db_session, "Epic Only")
    service.mark_checked(db_session, [g3.id], "steam", "US", T + timedelta(days=2))
    service.mark_checked(db_session, [g2.id], "steam", "US", T)
    service.mark_checked(db_session, [g1.id], "steam", "GB", T)  # another region does not count
    due = bulk.games_due(db_session, "US")
    assert [gid for gid, _, _ in due] == [g1.id, g2.id, g3.id]
    assert due[0][1:] == (1000, "Game 0")


def test_refresh_records_prices_and_marks_every_game_checked(db_session):
    g1, g2 = add_games(db_session, 2)
    fake = FakeSteam({1000: priced(1000, 999)})  # game 1001 has no price (free to play)
    rep = bulk.refresh_steam_prices(db_session, "US", connector=fake, batch_size=10)
    assert (rep["priced"], rep["no_price"], rep["snapshots"], rep["batches"]) == (1, 1, 1, 1)
    assert db_session.query(PriceSnapshot).one().price_cents == 999
    assert {c.game_id for c in db_session.query(PriceCheck)} == {g1.id, g2.id}
    assert db_session.query(JobRun).filter_by(store_id="steam", outcome="ok").count() == 1


def test_an_unchanged_price_is_not_stored_again_on_the_next_run(db_session):
    add_games(db_session, 1)
    fake = FakeSteam({1000: priced(1000)})
    bulk.refresh_steam_prices(db_session, "US", connector=fake)
    rep = bulk.refresh_steam_prices(db_session, "US", connector=fake)
    assert rep["priced"] == 1 and rep["snapshots"] == 0
    assert db_session.query(PriceSnapshot).count() == 1


def test_games_are_sent_in_batches_of_the_requested_size(db_session):
    add_games(db_session, 5)
    fake = FakeSteam({})
    bulk.refresh_steam_prices(db_session, "US", connector=fake, batch_size=2)
    assert [len(c) for c in fake.calls] == [2, 2, 1]


def test_a_failing_batch_is_split_in_half_and_retried(db_session):
    add_games(db_session, 4)

    def fail(ids, n):
        if len(ids) > 2:
            raise ConnectorError("too many ids")

    fake = FakeSteam({a: priced(a) for a in range(1000, 1004)}, fail)
    rep = bulk.refresh_steam_prices(db_session, "US", connector=fake, batch_size=4)
    assert [len(c) for c in fake.calls] == [4, 2, 2]
    assert rep["priced"] == 4 and rep["errors"] == 0


def test_a_single_game_that_keeps_failing_is_skipped_and_not_marked_checked(db_session):
    g0, g1, g2 = add_games(db_session, 3)

    def fail(ids, n):
        if 1001 in ids:
            raise ConnectorError("boom")

    fake = FakeSteam({a: priced(a) for a in (1000, 1001, 1002)}, fail)
    rep = bulk.refresh_steam_prices(db_session, "US", connector=fake, batch_size=1)
    assert rep["errors"] == 1 and rep["priced"] == 2
    assert {c.game_id for c in db_session.query(PriceCheck)} == {g0.id, g2.id}
    assert db_session.query(JobRun).filter_by(store_id="steam", outcome="error").count() == 1


def test_rate_limit_stops_the_run_and_the_unchecked_games_go_first_next_time(db_session):
    g0, g1, g2 = add_games(db_session, 3)

    def fail(ids, n):
        if n == 2:
            raise RateLimited("steam rate limited (HTTP 429)")

    fake = FakeSteam({a: priced(a) for a in (1000, 1001, 1002)}, fail)
    blocked = set()
    rep = bulk.refresh_steam_prices(db_session, "US", connector=fake, batch_size=1, blocked=blocked)
    assert rep["rate_limited"] is True and "steam" in blocked
    assert len(fake.calls) == 2  # the third batch was never attempted
    assert {c.game_id for c in db_session.query(PriceCheck)} == {g0.id}
    assert db_session.query(JobRun).filter_by(store_id="steam", outcome="rate_limited").count() == 1
    assert [gid for gid, _, _ in bulk.games_due(db_session, "US")][:2] == [g1.id, g2.id]


def test_a_store_already_blocked_this_run_is_not_called(db_session):
    add_games(db_session, 2)
    fake = FakeSteam({})
    rep = bulk.refresh_steam_prices(db_session, "US", connector=fake, blocked={"steam"})
    assert fake.calls == [] and rep["rate_limited"] is True


class ProbeSteam:
    """Returns entries only for the first `limit` ids, like a server that silently truncates."""

    def __init__(self, limit):
        self.limit, self.sizes = limit, []

    def fetch_batch_payload(self, app_ids, region="US"):
        self.sizes.append(len(app_ids))
        return {str(a): {"success": True, "data": []} for a in app_ids[:self.limit]}


def test_probe_returns_100_when_the_server_fully_answers_both_sizes():
    fake = ProbeSteam(limit=120)
    assert bulk.probe_batch_size(fake, list(range(1, 301))) == 100
    assert fake.sizes == [50, 100]


def test_probe_returns_50_when_the_server_truncates_a_batch_of_100():
    fake = ProbeSteam(limit=70)
    assert bulk.probe_batch_size(fake, list(range(1, 301))) == 50
    assert fake.sizes == [50, 100]


def test_probe_returns_zero_when_even_the_smallest_size_fails():
    fake = ProbeSteam(limit=10)
    assert bulk.probe_batch_size(fake, list(range(1, 301))) == 0


def test_probe_ignores_sizes_larger_than_the_sample():
    fake = ProbeSteam(limit=1000)
    assert bulk.probe_batch_size(fake, list(range(1, 76))) == 50
    assert fake.sizes == [50]
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_bulk.py -q`
Expected: FAIL (`ImportError: cannot import name 'bulk'`).

- [ ] **Step 3: Implement**

Create `price-match/app/bulk.py`:

```python
"""Bulk Steam price refresh for the whole catalog.

Games are sent to Steam in batches. Never-checked games go first, then the least
recently checked, so a run cut short (rate limit, restart) resumes where it stopped.
Every game in a successful batch is marked checked, including those Steam returned no
price for (free to play, unreleased, delisted), so they are not retried forever.

    python -m app.bulk --probe     # find the largest batch size Steam fully answers
"""
from __future__ import annotations

import logging
from collections import deque

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config, service
from .connectors.base import RateLimited
from .connectors.steam import SteamConnector
from .models import Game, PriceCheck, utcnow

log = logging.getLogger("playmatch.bulk")


def games_due(s: Session, region: str) -> list[tuple[int, int, str]]:
    """(game id, steam app id, title): never-checked first, then oldest check."""
    q = (select(Game.id, Game.steam_app_id, Game.title)
         .outerjoin(PriceCheck, (PriceCheck.game_id == Game.id)
                    & (PriceCheck.store_id == "steam") & (PriceCheck.region == region))
         .where(Game.steam_app_id.is_not(None))
         .order_by(PriceCheck.checked_at.asc().nulls_first(), Game.id))
    return [(gid, aid, title) for gid, aid, title in s.execute(q)]


def refresh_steam_prices(s: Session, region: str = "US", connector=None,
                         batch_size: int | None = None, blocked: set[str] | None = None) -> dict:
    connector = connector or SteamConnector()
    size = max(1, batch_size or config.STEAM_BATCH_SIZE)
    blocked = blocked if blocked is not None else set()
    rep = {"games": 0, "batches": 0, "priced": 0, "no_price": 0, "snapshots": 0,
           "errors": 0, "rate_limited": False}
    if "steam" in blocked:
        rep["rate_limited"] = True
        return rep
    due = games_due(s, region)
    rep["games"] = len(due)
    pending = deque(due[i:i + size] for i in range(0, len(due), size))
    while pending:
        batch = pending.popleft()
        ids = [app_id for _, app_id, _ in batch]
        try:
            result = connector.prices_for(ids, region)
        except RateLimited as e:
            blocked.add("steam")
            rep["rate_limited"] = True
            log.error("steam rate limited us; halting Steam for this run: %s", e)
            service.log_job(s, "ingest", "steam", region, "rate_limited", str(e))
            break
        except Exception as e:
            if len(batch) > 1:  # an oversized or bad batch: retry as two halves
                mid = len(batch) // 2
                pending.appendleft(batch[mid:])
                pending.appendleft(batch[:mid])
                continue
            rep["errors"] += 1
            log.warning("steam batch of 1 failed (app %s): %s", ids[0], e)
            service.log_job(s, "ingest", "steam", region, "error", str(e))
            continue
        _record_batch(s, batch, result, region, rep)
    return rep


def _record_batch(s: Session, batch, result: dict, region: str, rep: dict) -> None:
    now = utcnow()
    games = {g.id: g for g in s.scalars(select(Game).where(Game.id.in_([gid for gid, _, _ in batch])))}
    for game_id, app_id, title in batch:
        raw = result.get(app_id)
        if raw is None:
            rep["no_price"] += 1
            continue
        raw.title, raw.region = title, region
        rep["priced"] += 1
        if service.record_snapshot_if_due(s, games[game_id], raw, now):
            rep["snapshots"] += 1
    service.mark_checked(s, [gid for gid, _, _ in batch], "steam", region, now)  # commits
    service.log_job(s, "ingest", "steam", region, "ok")
    rep["batches"] += 1


def probe_batch_size(connector, app_ids: list[int], region: str = "US",
                     sizes=(50, 100)) -> int:
    """Largest tested size for which Steam returned an entry for at least 95% of the ids
    asked for. Stops at the first size that is not fully answered. 0 if even the smallest fails."""
    best = 0
    for size in sizes:
        if size > len(app_ids):
            break
        try:
            payload = connector.fetch_batch_payload(app_ids[:size], region)
        except Exception as e:
            log.info("probe: size %d failed: %s", size, e)
            break
        if len(payload) < 0.95 * size:
            break
        best = size
    return best


def main():
    import argparse

    from .db import SessionLocal, init_db

    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="find the largest batch size Steam fully answers")
    ap.add_argument("--region", default="US")
    a = ap.parse_args()
    if not a.probe:
        ap.error("nothing to do; use --probe (the daily job runs the refresh itself)")
    init_db()
    with SessionLocal() as s:
        ids = [aid for _, aid, _ in games_due(s, a.region)][:100]
    if len(ids) < 50:
        raise SystemExit(f"need at least 50 games with a Steam id in the database (have {len(ids)}); run catalog sync first")
    best = probe_batch_size(SteamConnector(), ids, a.region)
    print(f"largest batch size fully answered: {best}" if best else "even a batch of 50 was not fully answered")
    print(f"set STEAM_BATCH_SIZE={best} in .env" if best else "keep STEAM_BATCH_SIZE at a smaller value, e.g. 20")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_bulk.py -q`
Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: resumable bulk Steam price refresh with a batch-size probe" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 6: Rewire the daily job

**Files:**
- Modify: `price-match/app/snapshot.py`
- Create: `price-match/tests/test_pipeline.py`

**Interfaces:**
- Consumes: `catalog.sync(s)`, `bulk.refresh_steam_prices(s, region, blocked=...)`, `service.ingest_game(s, game, region, connectors=[...], blocked=...)`, `service.mark_checked`, `gogdb.import_archive(s, path, review_cap=...)` (added in Task 7; the pipeline test fakes it), `config.REVIEW_QUEUE_CAP_PER_RUN`, `connectors.epic.EpicConnector`.
- Produces: `snapshot.curated_app_ids() -> set[int]`; `snapshot.run(seed: bool = False, live: bool = True, gog: bool = True, catalog_sync: bool = True) -> None`; CLI flag `--skip-catalog`.

- [ ] **Step 1: Write the failing tests**

Create `price-match/tests/test_pipeline.py`:

```python
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import config, snapshot
from app.db import Base
from app.models import Game, PriceCheck


@pytest.fixture()
def wired(monkeypatch, tmp_path):
    """snapshot.run against an in-memory database with every outbound step replaced by a recorder."""
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from app import models  # noqa: F401
    Base.metadata.create_all(eng)
    monkeypatch.setattr(snapshot, "SessionLocal", sessionmaker(bind=eng, expire_on_commit=False))
    monkeypatch.setattr(snapshot, "init_db", lambda: None)
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps([{"title": "Hades", "steam_app_id": 1145360, "year": 2020}]))
    monkeypatch.setattr(snapshot, "SEED", seed)

    calls, epic = [], {"outcome": "ok"}
    monkeypatch.setattr(snapshot.fx, "refresh", lambda s: calls.append("fx"))
    monkeypatch.setattr(snapshot.catalog, "sync", lambda s: calls.append("catalog") or {})
    monkeypatch.setattr(snapshot.bulk, "refresh_steam_prices",
                        lambda s, region, blocked=None: calls.append(("steam", region)) or {})

    def ingest(s, game, region, connectors=None, blocked=None):
        calls.append(("epic", game.title, [c.store_id for c in connectors]))
        return {"stores": {"epic": epic["outcome"]}}

    monkeypatch.setattr(snapshot.service, "ingest_game", ingest)
    monkeypatch.setattr(snapshot.gogdb, "download_latest", lambda: "archive")
    monkeypatch.setattr(snapshot.gogdb, "import_archive",
                        lambda s, path, review_cap=None: calls.append(("gog", path, review_cap)) or {})
    return calls, epic, eng


def test_daily_pipeline_order_and_epic_only_for_seed_games(wired):
    calls, _, eng = wired
    with sessionmaker(bind=eng)() as s:
        s.add(Game(title="Other", norm_title="other", steam_app_id=5))  # in the catalog, not a seed game
        s.commit()
    snapshot.run(seed=True)
    assert calls == ["catalog", "fx", ("steam", "US"), ("epic", "Hades", ["epic"]),
                     ("gog", "archive", config.REVIEW_QUEUE_CAP_PER_RUN)]


def test_epic_seed_refresh_marks_the_game_checked(wired):
    _, _, eng = wired
    snapshot.run(seed=True)
    with sessionmaker(bind=eng)() as s:
        assert s.query(PriceCheck).filter_by(store_id="epic").count() == 1


def test_a_failed_epic_lookup_is_not_marked_checked(wired):
    _, epic, eng = wired
    epic["outcome"] = "error: boom"
    snapshot.run(seed=True)
    with sessionmaker(bind=eng)() as s:
        assert s.query(PriceCheck).filter_by(store_id="epic").count() == 0


def test_a_no_match_epic_lookup_is_marked_checked(wired):
    _, epic, eng = wired
    epic["outcome"] = "no confident match"
    snapshot.run(seed=True)
    with sessionmaker(bind=eng)() as s:
        assert s.query(PriceCheck).filter_by(store_id="epic").count() == 1


def test_steps_can_be_skipped(wired):
    calls, _, _ = wired
    snapshot.run(seed=True, live=False, gog=False, catalog_sync=False)
    assert calls == ["fx"]
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_pipeline.py -q`
Expected: FAIL (`AttributeError: module 'app.snapshot' has no attribute 'catalog'`).

- [ ] **Step 3: Implement**

Replace the imports, `run` and `main` in `price-match/app/snapshot.py` (keep the module docstring, but update its usage lines as shown):

```python
"""Daily data job: catalog sync, FX refresh, bulk Steam prices, Epic for seed games, GOGDB import.

    python -m app.snapshot                # everything
    python -m app.snapshot --seed         # load seed_games.json first
    python -m app.snapshot --skip-catalog # do not list Steam games (no API key needed)
    python -m app.snapshot --skip-gogdb   # live stores only
    python -m app.snapshot --skip-live    # GOGDB only
"""
import argparse
import json
import logging
import pathlib

from sqlalchemy import select

from . import bulk, catalog, config, fx, gogdb, regions, service
from .connectors.epic import EpicConnector
from .db import SessionLocal, init_db
from .models import Game

SEED = pathlib.Path(__file__).with_name("seed_games.json")
log = logging.getLogger("playmatch.job")


def curated_app_ids() -> set[int]:
    """Steam ids of the seed games: the only games whose Epic price the daily job refreshes."""
    return {g["steam_app_id"] for g in json.loads(SEED.read_text()) if g.get("steam_app_id")}


def run(seed=False, live=True, gog=True, catalog_sync=True) -> None:
    init_db()
    with SessionLocal() as s:
        service.seed_stores(s)
        if seed:
            for g in json.loads(SEED.read_text()):
                service.upsert_game(s, g["title"], g.get("steam_app_id"), g.get("year"))
        if catalog_sync:
            print(catalog.sync(s))
        try:
            fx.refresh(s)
        except Exception as e:
            log.warning("fx refresh failed, using stored rates: %s", e)
        if live:
            blocked: set[str] = set()  # shared: a rate-limited store is skipped for the rest of the run
            curated = curated_app_ids()
            for region in regions.enabled():
                print(bulk.refresh_steam_prices(s, region.code, blocked=blocked))
                for g in s.scalars(select(Game).where(Game.steam_app_id.in_(curated))).all():
                    report = service.ingest_game(s, g, region.code, connectors=[EpicConnector()],
                                                 blocked=blocked)
                    print(report)
                    if report["stores"].get("epic") in ("ok", "no confident match"):
                        service.mark_checked(s, [g.id], "epic", region.code)
        if gog:
            try:
                print(gogdb.import_archive(s, gogdb.download_latest(),
                                           review_cap=config.REVIEW_QUEUE_CAP_PER_RUN))
            except Exception as e:
                log.error("GOGDB import failed: %s", e)
                service.log_job(s, "gogdb_import", "gog", "-", "error", str(e))


def main():
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--skip-catalog", action="store_true")
    ap.add_argument("--skip-live", action="store_true")
    ap.add_argument("--skip-gogdb", action="store_true")
    a = ap.parse_args()
    run(a.seed, not a.skip_live, not a.skip_gogdb, not a.skip_catalog)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_pipeline.py -q` then `python -m pytest -q`
Expected: 5 passed for the new file; full suite green apart from the known flaky test.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: run the daily job as catalog, bulk Steam prices, seed-only Epic, then GOG" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 7: GOG review-queue cap (and a pin on catalog-game matching)

**Files:**
- Modify: `price-match/app/gogdb.py` (`import_archive`)
- Create: `price-match/tests/test_gog_scale.py`

**Interfaces:**
- Consumes: `gogdb.import_archive`, `service.queue_review`, and the test helper `make_archive` from `tests/test_core.py` (a GOG archive containing Hades 2020 and Celeste 2018 plus a soundtrack and a DLC).
- Produces: `gogdb.import_archive(s, path, region_list=None, review_cap: int | None = None) -> dict`. The report gains `review_dropped`. Near-matches are collected during the run, sorted by score (highest first), and only the first `review_cap` are queued (`None` = all, the old behaviour).

- [ ] **Step 1: Write the failing tests**

Create `price-match/tests/test_gog_scale.py`:

```python
from app import gogdb, service
from app.models import Listing, MatchCandidate
from tests.test_core import make_archive


def test_a_catalog_game_with_an_exact_title_auto_matches_even_with_no_release_year(db_session, tmp_path):
    hades = service.upsert_game(db_session, "Hades", 1145360)  # catalog games have no year
    rep = gogdb.import_archive(db_session, make_archive(tmp_path))
    assert rep["matched"] == 1
    assert db_session.query(Listing).filter_by(game_id=hades.id, store_id="gog").count() >= 1


def test_a_near_title_with_no_release_year_is_queued_for_review_not_auto_matched(db_session, tmp_path):
    # The matcher's fuzzy auto-match needs both years. Catalog games have none, so near-matches
    # are reviewed rather than linked. This pins that consequence of the design.
    service.upsert_game(db_session, "Hadess", 1145360)
    rep = gogdb.import_archive(db_session, make_archive(tmp_path))
    assert rep["matched"] == 0 and rep["review_queued"] == 1 and rep["review_dropped"] == 0
    assert db_session.query(MatchCandidate).filter_by(status="pending").count() == 1


def test_the_review_cap_keeps_the_highest_scores(db_session, tmp_path):
    # "Hades" (year 2005 vs GOG's 2020) scores 90 (exact title, year mismatch);
    # "Hadess" scores about 91 (fuzzy). Cap the queue at one: the higher score wins.
    service.upsert_game(db_session, "Hades", 111, 2005)
    service.upsert_game(db_session, "Hadess", 222)
    rep = gogdb.import_archive(db_session, make_archive(tmp_path), review_cap=1)
    assert rep["review_queued"] == 1 and rep["review_dropped"] == 1
    queued = db_session.query(MatchCandidate).one()
    assert queued.score > 90


def test_a_zero_cap_queues_nothing(db_session, tmp_path):
    service.upsert_game(db_session, "Hades", 111, 2005)
    rep = gogdb.import_archive(db_session, make_archive(tmp_path), review_cap=0)
    assert rep["review_queued"] == 0 and rep["review_dropped"] == 1
    assert db_session.query(MatchCandidate).count() == 0


def test_no_cap_queues_everything_as_before(db_session, tmp_path):
    service.upsert_game(db_session, "Hades", 111, 2005)
    service.upsert_game(db_session, "Hadess", 222)
    rep = gogdb.import_archive(db_session, make_archive(tmp_path))
    assert rep["review_queued"] == 2 and rep["review_dropped"] == 0
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_gog_scale.py -q`
Expected: FAIL (`TypeError: import_archive() got an unexpected keyword argument 'review_cap'` and `KeyError: 'review_dropped'`).

- [ ] **Step 3: Implement**

In `price-match/app/gogdb.py`, change the signature and report:

```python
def import_archive(s: Session, path: Path, region_list=None, review_cap: int | None = None) -> dict:
```

```python
    report = {"archive": path.name, "products": len(products), "products_with_prices": len(prices),
              "files_seen": seen, "matched": 0, "snapshots_added": 0, "review_queued": 0,
              "review_dropped": 0}
```

Just before `for game in s.scalars(select(Game)).all():` add:

```python
    reviews = []  # (score, game, raw): queued after the loop, best first, up to review_cap
```

Replace the `elif res.verdict == "review":` block inside the loop:

```python
            elif res.verdict == "review":
                latest = next(iter(prices[p["id"]].values()))[-1]
                reviews.append((res.score, game, RawListing(
                    "gog", p["id"], p["title"], p["url"], latest[3], latest[2], latest[1],
                    region=next(iter(prices[p["id"]])), release_year=p["year"])))
```

Directly before the final `log_job(s, "gogdb_import", ...)` call add:

```python
    reviews.sort(key=lambda r: -r[0])
    keep = reviews if review_cap is None else reviews[:max(0, review_cap)]
    for score, game, raw in keep:
        queue_review(s, game, raw, score)
    report["review_queued"] = len(keep)
    report["review_dropped"] = len(reviews) - len(keep)
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_gog_scale.py -q` then `python -m pytest -q`
Expected: 5 passed; the existing `test_gogdb_import_and_idempotence` still passes.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: cap the GOG review queue per run, highest scores first" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 8: Scale benchmark and the two decision rules

**Files:**
- Create: `price-match/scripts/__init__.py` (empty)
- Create: `price-match/scripts/scale_test.py`
- Modify (only if a decision rule fires): `price-match/app/db.py`, `price-match/app/gogdb.py`, `price-match/requirements.txt`

**Interfaces:**
- Consumes: `models.Game`, `db.SessionLocal/init_db`, `matcher.normalize`, rapidfuzz.
- Produces: `python -m scripts.scale_test populate|search|match`. Plan 2's browser checks reuse `populate`. Recorded results feed two decisions: add a `pg_trgm` index if a `contains` search takes more than 200 ms on Postgres; switch GOG matching to chunked `cdist` if the projected 100,000-game import takes more than 600 seconds.

- [ ] **Step 1: Write the script**

Create an empty `price-match/scripts/__init__.py`, then create `price-match/scripts/scale_test.py`:

```python
"""Scale checks for the full-catalog design. Run from price-match/.

    DATABASE_URL=sqlite:///scale.db python -m scripts.scale_test populate --games 100000
    DATABASE_URL=... python -m scripts.scale_test search
    python -m scripts.scale_test match --games 100000 --pool 12000 --sample 3000

`search` times the exact query the API runs. `match` times the GOG fuzzy-matching call on a
sample of games and projects the time for the full catalog, and (if numpy is installed) also
times a chunked `cdist` alternative.
"""
import argparse
import random
import time

WORDS = ("dragon knight star war city night blade lost world last hero ghost quest rogue "
         "farm space craft dark soul witch royal iron sky sea fire ice storm shadow legend "
         "tower island road ship train zombie robot kingdom empire arena racing puzzle hunter").split()


def fake_title(rng):
    return " ".join(rng.choice(WORDS) for _ in range(rng.randint(2, 4)))


def populate(n):
    from app.db import SessionLocal, init_db
    from app.matcher import normalize
    from app.models import Game
    init_db()
    rng = random.Random(1)
    with SessionLocal() as s:
        have = s.query(Game).count()
        for start in range(0, n, 10000):
            rows = []
            for i in range(start, min(n, start + 10000)):
                t = fake_title(rng)
                rows.append({"title": t.title(), "norm_title": normalize(t), "steam_app_id": 9_000_000 + have + i})
            s.bulk_insert_mappings(Game, rows)
            s.commit()
        print(f"games in database: {s.query(Game).count()}")


def search():
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models import Game
    with SessionLocal() as s:
        for q in ("dragon", "dark soul", "zzzz", "a"):
            t0 = time.perf_counter()
            rows = s.scalars(select(Game).where(Game.norm_title.contains(q)).order_by(Game.title, Game.id).limit(48)).all()
            ms = (time.perf_counter() - t0) * 1000
            print(f"search {q!r:12} {len(rows):3} rows  {ms:8.1f} ms  {'SLOW (over 200 ms)' if ms > 200 else 'ok'}")


def match(games, pool, sample):
    from rapidfuzz import fuzz, process
    rng = random.Random(2)
    game_norms = [fake_title(rng) for _ in range(sample)]
    pool_norms = [fake_title(rng) for _ in range(pool)]
    t0 = time.perf_counter()
    for g in game_norms:
        process.extract(g, pool_norms, scorer=fuzz.ratio, limit=5, score_cutoff=85)
    per_game = (time.perf_counter() - t0) / sample
    proj = per_game * games
    print(f"extract loop: {per_game * 1000:.2f} ms/game -> {proj:.0f} s projected for {games} games "
          f"{'OVER 600 s: use chunked cdist' if proj > 600 else 'ok'}")
    try:
        import numpy as np
    except ImportError:
        print("numpy not installed; skipping the cdist comparison")
        return
    t0 = time.perf_counter()
    for i in range(0, sample, 500):
        process.cdist(game_norms[i:i + 500], pool_norms, scorer=fuzz.ratio, score_cutoff=85,
                      dtype=np.uint8, workers=-1)
    per_game = (time.perf_counter() - t0) / sample
    print(f"chunked cdist: {per_game * 1000:.2f} ms/game -> {per_game * games:.0f} s projected")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["populate", "search", "match"])
    ap.add_argument("--games", type=int, default=100000)
    ap.add_argument("--pool", type=int, default=12000)
    ap.add_argument("--sample", type=int, default=3000)
    a = ap.parse_args()
    {"populate": lambda: populate(a.games), "search": search,
     "match": lambda: match(a.games, a.pool, min(a.sample, a.games))}[a.cmd]()
```

- [ ] **Step 2: Run the matching benchmark**

Run (from `price-match/`): `python -m scripts.scale_test match --games 100000 --pool 12000 --sample 3000`
Expected: prints the projected seconds for the extract loop. If numpy is missing, run `pip install numpy` first to also get the `cdist` figure. Record both numbers.

- [ ] **Step 3: Run the search benchmark against Postgres**

Start a throwaway Postgres (port 5433 so it does not collide with anything) and fill it:

```bash
docker run -d --rm --name pm-scale-pg -e POSTGRES_PASSWORD=pm -e POSTGRES_USER=pm -e POSTGRES_DB=pm -p 5433:5432 postgres:16-alpine
```

```bash
export DATABASE_URL=postgresql+psycopg://pm:pm@localhost:5433/pm
python -m scripts.scale_test populate --games 100000
python -m scripts.scale_test search
```

(On PowerShell, set `$env:DATABASE_URL = "postgresql+psycopg://pm:pm@localhost:5433/pm"` instead of `export`.)
Expected: four lines of timings and `ok` or `SLOW`. Record the numbers.

- [ ] **Step 4: Decision rule A: search index (only if any search printed SLOW)**

If every search is `ok`, skip to Step 6. Otherwise, add to `price-match/app/db.py`:

```python
def init_db():
    from . import models  # noqa: F401

    Base.metadata.create_all(engine)
    if engine.dialect.name == "postgresql":
        # Makes the API's substring search (LIKE '%text%') an index lookup at 100k+ games.
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_games_norm_title_trgm "
                                 "ON games USING gin (norm_title gin_trgm_ops)")
```

Add a test to `price-match/tests/test_foundations.py`:

```python
def test_init_db_is_a_noop_extra_on_sqlite(monkeypatch):
    from app import db
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", eng)
    db.init_db()  # must not try to run Postgres-only SQL
```

Run `python -m pytest tests/test_foundations.py -q` (expect pass), then re-run Step 3's `search` and confirm every line is `ok`.

- [ ] **Step 5: Decision rule B: chunked GOG matching (only if the extract loop printed OVER 600 s)**

If it printed `ok`, skip to Step 6. Otherwise add `numpy>=1.26` to `price-match/requirements.txt` and add this to `price-match/app/gogdb.py` (below the imports):

```python
def top_matches(queries: list[str], choices: list[str], limit: int = 5, cutoff: int = 85, chunk: int = 500):
    """For each query, up to `limit` (score, index) pairs at or above `cutoff`, best first.
    Same results as `process.extract` per query, computed in numpy-backed chunks."""
    import numpy as np
    for start in range(0, len(queries), chunk):
        block = process.cdist(queries[start:start + chunk], choices, scorer=fuzz.ratio,
                              score_cutoff=cutoff, dtype=np.uint8, workers=-1)
        for row in block:
            idx = np.nonzero(row)[0]
            best = idx[np.argsort(-row[idx], kind="stable")][:limit]
            yield [(int(row[i]), int(i)) for i in best]
```

Add the equivalence test to `price-match/tests/test_gog_scale.py`:

```python
def test_top_matches_agrees_with_extract():
    from rapidfuzz import fuzz, process
    choices = ["hades", "hades soundtrack", "celeste", "hadess", "dark soul", "dark souls 3"]
    queries = ["hades", "hadess", "dark souls", "nothing like it"]
    expected = [[(int(sc), i) for _, sc, i in process.extract(q, choices, scorer=fuzz.ratio, limit=5, score_cutoff=85)]
                for q in queries]
    assert list(gogdb.top_matches(queries, choices)) == expected
```

Then change the loop in `import_archive` to compute candidates once, before the `for game in ...` loop, and use them:

```python
    all_games = s.scalars(select(Game)).all()
    tops = top_matches([g.norm_title for g in all_games], norm)
    for game, top in zip(all_games, tops):
```

and replace `for _, score, idx in process.extract(game.norm_title, norm, scorer=fuzz.ratio, limit=5, score_cutoff=85):` with `for score, idx in top:`. Run `python -m pytest -q`; the existing GOG tests must still pass. Re-run the Step 2 benchmark's cdist figure and confirm the projection is under 600 seconds.

- [ ] **Step 6: Clean up and record the results**

```bash
docker rm -f pm-scale-pg
```

Delete the temporary `scale.db` if you created one. Add the recorded numbers to the README section written in Task 9 (Step 1 there has a placeholder table to fill: it is filled with these measured numbers, not left blank).

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "test: add scale benchmark script and apply its decision rules" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 9: README and final verification

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: everything above.
- Produces: documentation of the new pipeline, settings and first-run procedure.

- [ ] **Step 1: Update the README**

In `README.md`:

1. In the `## Layout` list, change the `price-match/` line to mention the new modules:
   `- `price-match/` FastAPI service: connectors, matcher, FX, catalog sync, bulk Steam prices, GOGDB importer, daily job, price + history + stats API, `/metrics``
2. Replace the `## Data sources` table's Steam row with: `| Steam | Catalog: `IStoreService/GetAppList` (needs `STEAM_API_KEY`). Prices: batched `appdetails` calls, several games per request | Unofficial price endpoint. `REQUEST_DELAY` spacing applies per request |`
3. Add this section after `## Data sources`:

```markdown
## Full Steam catalog
The daily job (`python -m app.snapshot`) runs, in order: catalog sync (lists every Steam base game), FX refresh, bulk Steam prices, Epic prices for the seed games only, and the GOGDB import.

- **Catalog:** needs a free Steam Web API key (https://steamcommunity.com/dev/apikey) in `.env` as `STEAM_API_KEY`. Without it the step is skipped and the existing games keep working. Two Steam apps with the same title stay two games.
- **Steam prices:** `STEAM_BATCH_SIZE` games per request (default 50). A batch that fails is split in half and retried. A game is marked checked (table `price_checks`) even when Steam returns no price (free to play, unreleased, delisted). Never-checked games are fetched first, so a run cut short resumes where it stopped.
- **Snapshots** are stored only when a price changes, plus one every `SNAPSHOT_HEARTBEAT_DAYS` (default 7). The chart already treats a price as holding until the next sample.
- **GOG for catalog games:** catalog games have no release year, so only exact title matches (after normalizing) link automatically. Near-matches go to the manual review queue, at most `REVIEW_QUEUE_CAP_PER_RUN` (default 200) per run, highest score first.
- **Epic:** only seed games (`app/seed_games.json`) are refreshed daily. Other games are looked up when someone opens them (see the API section).

### First run with a key
    docker compose up -d --build
    docker compose run --rm snapshot --seed --skip-gogdb     # lists every game, prices them all
    docker compose run --rm --entrypoint python snapshot -m app.bulk --probe

The first command prints the catalog sync result (`{"added": N, ...}`, which is your game count) and the bulk report. If `catalog.sync` printed `{"skipped": "error: ..."}` the API key was rejected or Steam's response shape differs from the parser: check the log line. The probe prints the largest batch size Steam fully answers; set `STEAM_BATCH_SIZE` to it in `.env`.

### Scale results
Measured on a 100,000-game database (see `scripts/scale_test.py`):

| Check | Result |
| --- | --- |
| Substring search on Postgres (4 queries) | *(fill from Task 8 Step 3)* |
| GOG matching, projected for 100,000 games | *(fill from Task 8 Step 2)* |
| Index / matching change applied | *(none, or `pg_trgm` index / chunked `cdist`)* |
```

Then replace each italic cell with the numbers recorded in Task 8. Every cell must contain a real value or the words "not measured" plus the reason.

4. Update the `## Known limits` list with:
   `- Non-seed games get Epic prices only when opened, so their Epic history is sparse.`
   `- A game delisted from Steam keeps its last price; the profile page shows when it was last checked.`

- [ ] **Step 2: Run the whole suite**

Run (from `price-match/`): `python -m pytest -q`
Expected: every test passes (the known flaky test excepted; rerun once if it is the only failure).

- [ ] **Step 3: Verify the Docker image builds**

Run (from `C:\Users\vetsa\playmatch`): `docker compose build price-match`
Expected: the build succeeds (nothing new is installed unless Task 8 Step 5 added numpy).

- [ ] **Step 4: Smoke-run the job end to end without a key**

Run (from `price-match/`, PowerShell shown; use `export` in bash):

```powershell
$env:DATABASE_URL = "sqlite:///smoke.db"
python -m app.snapshot --seed --skip-live --skip-gogdb
```

Expected: prints `{'skipped': 'no STEAM_API_KEY'}` and exits 0 without a traceback; the seed games are in the database. Then delete `smoke.db`.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "docs: document the full Steam catalog pipeline and its first run" -m "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Self-review (run against the spec)

- **Catalog sync:** Task 3 (paging, filters, rename, no-key and error skips, no merge by title).
- **Bulk Steam prices:** Tasks 4 and 5 (batched request, halving, resume order, rate-limit breaker, no-price games checked, unchanged skipped) plus the probe.
- **Change-only snapshots and heartbeat:** Task 2.
- **`price_checks` table:** Task 1 (used by Tasks 5 and 6, and by Plan 2).
- **Epic for seed games only, marked checked:** Task 6.
- **GOG at scale, review cap, benchmark and decision rules:** Tasks 7 and 8.
- **Configuration, `.env.example`, compose, README:** Tasks 1 and 9.
- **Not in this plan (in Plan 2):** API paging and `X-Total-Count`, `checked_at` in the prices response, the `epic-check` endpoint and limiter, all UI changes, and the browser checks.
- **Type consistency:** `mark_checked(s, game_ids, store_id, region, now)`, `record_snapshot_if_due(s, game, raw, now)`, `games_due(s, region) -> list[(game_id, app_id, title)]`, `refresh_steam_prices(s, region, connector, batch_size, blocked)` and `import_archive(..., review_cap)` are used with identical signatures in every task and test that references them.
