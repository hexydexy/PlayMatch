from sqlalchemy.exc import IntegrityError

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


def _boom(*_a, **_k):
    raise IntegrityError("insert", {}, Exception("UNIQUE constraint failed"))


def test_a_racing_listing_insert_for_the_same_game_reports_fresh_not_500(api, db_session, monkeypatch):
    """Two requests for a never-checked game can both pass the cooldown check and race to
    insert the same listing; the loser's write collides on the unique constraint and must
    report `fresh` (as if the other request's check happened just first), not 500."""
    g = service.upsert_game(db_session, "Hades", 1145360, 2020)
    monkeypatch.setattr(service, "record_snapshot", _boom)
    wire(monkeypatch, FakeEpic([epic_hades()]))
    r = api.post(f"/api/games/{g.id}/epic-check")
    assert r.status_code == 200
    assert r.json()["status"] == "fresh"


def test_a_racing_price_check_insert_for_the_same_game_reports_fresh_not_500(api, db_session, monkeypatch):
    """The listing insert can win while the price_checks insert still collides with the
    other request's own mark_checked; that must also report `fresh`, not 500."""
    g = service.upsert_game(db_session, "Hades", 1145360, 2020)
    monkeypatch.setattr(service, "mark_checked", _boom)
    wire(monkeypatch, FakeEpic([epic_hades()]))
    r = api.post(f"/api/games/{g.id}/epic-check")
    assert r.status_code == 200
    assert r.json()["status"] == "fresh"
