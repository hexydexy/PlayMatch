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
