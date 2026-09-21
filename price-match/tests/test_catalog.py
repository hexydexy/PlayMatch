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


def test_fetch_all_redacts_api_key_in_logs(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="httpx")

    def handler(req):
        return httpx.Response(200, json={"response": {
            "apps": [{"appid": 1, "name": "A"}], "have_more_results": False}})

    monkeypatch.setattr(catalog, "client", _mock(handler))
    catalog.fetch_all("SECRETKEY123")
    assert "SECRETKEY123" not in caplog.text
    assert "key=REDACTED" in caplog.text
    assert "include_games=true" in caplog.text
