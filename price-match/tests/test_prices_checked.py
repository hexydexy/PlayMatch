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


def test_a_snapshot_newer_than_the_price_check_wins(db_session):
    g = service.upsert_game(db_session, "Hades", 1145360, 2020)
    service.mark_checked(db_session, [g.id], "steam", "US", T)
    later = T + timedelta(days=20)
    service.record_snapshot_if_due(db_session, g, steam(), later)
    db_session.commit()
    p = service.current_prices(db_session, g, "USD", "US")
    assert p["prices"][0]["checked_at"] == later.isoformat()
    assert p["checks"]["steam"] == later.isoformat()
