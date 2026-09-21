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
