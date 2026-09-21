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
