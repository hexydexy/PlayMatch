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
