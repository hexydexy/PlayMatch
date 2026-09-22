import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import config, snapshot
from app.db import Base
from app.models import Game, JobRun, PriceCheck


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


def _job_errors(eng, **filters):
    with sessionmaker(bind=eng)() as s:
        return s.query(JobRun).filter_by(kind="daily_job", outcome="error", **filters).all()


def test_a_database_error_in_a_step_is_rolled_back_and_the_later_steps_still_run(wired, monkeypatch):
    calls, _, eng = wired

    def bulk_with_a_real_failing_flush(s, region, blocked=None):
        calls.append(("steam", region))
        s.add(Game(title="Dup", norm_title="dup", steam_app_id=1145360))  # duplicates the seed game's id
        s.flush()  # raises IntegrityError and leaves the session needing a rollback

    monkeypatch.setattr(snapshot.bulk, "refresh_steam_prices", bulk_with_a_real_failing_flush)
    snapshot.run(seed=True)
    assert calls == ["catalog", "fx", ("steam", "US"), ("epic", "Hades", ["epic"]),
                     ("gog", "archive", config.REVIEW_QUEUE_CAP_PER_RUN)]
    assert len(_job_errors(eng, store_id="steam")) == 1
    with sessionmaker(bind=eng)() as s:
        assert s.query(PriceCheck).filter_by(store_id="epic").count() == 1  # the shared session was usable again
        assert s.query(Game).count() == 1  # the failed insert did not stick


def test_a_plain_exception_in_a_step_is_isolated(wired, monkeypatch):
    calls, _, eng = wired

    def boom(s, region, blocked=None):
        raise RuntimeError("steam exploded")

    monkeypatch.setattr(snapshot.bulk, "refresh_steam_prices", boom)
    snapshot.run(seed=True)
    assert ("epic", "Hades", ["epic"]) in calls and calls[-1][0] == "gog"
    (err,) = _job_errors(eng, store_id="steam")
    assert "steam exploded" in err.detail


def test_a_failing_catalog_sync_does_not_stop_the_job(wired, monkeypatch):
    calls, _, eng = wired

    def boom(s):
        raise RuntimeError("catalog exploded")

    monkeypatch.setattr(snapshot.catalog, "sync", boom)
    snapshot.run(seed=True)
    assert calls == ["fx", ("steam", "US"), ("epic", "Hades", ["epic"]),
                     ("gog", "archive", config.REVIEW_QUEUE_CAP_PER_RUN)]
    assert len(_job_errors(eng, store_id="steam")) == 1


def test_one_seed_games_failed_epic_ingest_does_not_skip_the_next(wired, monkeypatch, tmp_path):
    calls, _, eng = wired
    seed = tmp_path / "seed2.json"
    seed.write_text(json.dumps([{"title": "Hades", "steam_app_id": 1145360, "year": 2020},
                                {"title": "Celeste", "steam_app_id": 504230, "year": 2018}]))
    monkeypatch.setattr(snapshot, "SEED", seed)

    def ingest(s, game, region, connectors=None, blocked=None):
        calls.append(("epic", game.title))
        if game.title == "Hades":
            raise RuntimeError("epic exploded")
        return {"stores": {"epic": "ok"}}

    monkeypatch.setattr(snapshot.service, "ingest_game", ingest)
    snapshot.run(seed=True)
    assert ("epic", "Hades") in calls and ("epic", "Celeste") in calls
    assert calls[-1][0] == "gog"
    assert len(_job_errors(eng, store_id="epic")) == 1
    with sessionmaker(bind=eng)() as s:
        checked = s.query(PriceCheck).filter_by(store_id="epic").all()
        assert len(checked) == 1  # only the game whose ingest succeeded was marked checked


def test_a_failing_gog_import_is_logged_and_rolled_back(wired, monkeypatch):
    _, _, eng = wired

    def bad_import(s, path, review_cap=None):
        s.add(Game(title="Dup", norm_title="dup", steam_app_id=1145360))
        s.flush()

    monkeypatch.setattr(snapshot.gogdb, "import_archive", bad_import)
    snapshot.run(seed=True)  # must not raise PendingRollbackError from log_job
    with sessionmaker(bind=eng)() as s:
        assert s.query(JobRun).filter_by(kind="gogdb_import", outcome="error").count() == 1
