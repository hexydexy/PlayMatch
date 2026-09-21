import gzip
import io
import json
import os
import tarfile

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import config, db, fx, gogdb, regions, service
from app.connectors import epic, steam
from app.connectors.base import RateLimited, RawListing
from app.matcher import evaluate, is_non_base, normalize
from app.models import FxRate, JobRun, Listing, MatchCandidate, PriceSnapshot, utcnow


@pytest.fixture()
def s():
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from app import models  # noqa
    db.Base.metadata.create_all(eng)
    with sessionmaker(bind=eng, expire_on_commit=False)() as sess:
        service.seed_stores(sess)
        sess.add_all([FxRate(base="USD", quote="EUR", rate=0.9, fetched_on="2026-09-19"),
                      FxRate(base="USD", quote="GBP", rate=0.75, fetched_on="2026-09-19")])
        sess.commit()
        yield sess


class Fake:
    def __init__(self, store_id, listings=None, exc=None):
        self.store_id, self.listings, self.exc, self.calls = store_id, listings or [], exc, 0

    def search(self, title, region="US"):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.listings


def raw(store, title, price, base=None, **kw):
    return RawListing(store_id=store, product_id=kw.pop("pid", title), title=title, url="http://x",
                      price_cents=price, base_price_cents=base or price, currency=kw.pop("currency", "USD"), **kw)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(config, "REQUEST_DELAY", 0)


# ---------------------------------------------------------------- matching

def test_normalize():
    assert normalize("The Witcher® 3: Wild Hunt™") == normalize("witcher 3 wild hunt")
    assert normalize("Final Fantasy VII") == "final fantasy 7"


def test_non_base_detection():
    assert is_non_base("Cyberpunk 2077 Ultimate Edition")
    assert is_non_base("Hades Soundtrack")
    assert not is_non_base("Hades")


def test_matcher_verdicts(s):
    g = service.upsert_game(s, "Hades", 1145360, 2020)
    assert evaluate(g, raw("gog", "Hades", 100, release_year=2020)).verdict == "match"
    assert evaluate(g, raw("gog", "Hades Soundtrack", 100)).verdict == "reject"
    assert evaluate(g, raw("gog", "Hades", 100, release_year=2005)).verdict == "review"
    assert evaluate(g, raw("gog", "Hades II", 100, release_year=2024)).verdict == "reject"
    assert evaluate(g, raw("steam", "Other", 100, steam_app_id=1)).verdict == "reject"


# ---------------------------------------------------------------- ingest

def test_ingest_partial_failure_and_logging(s):
    g = service.upsert_game(s, "Hades", 1145360, 2020)
    conns = [
        Fake("steam", [raw("steam", "Hades", 1999, 1999, pid="1145360", steam_app_id=1145360)]),
        Fake("epic", exc=RuntimeError("boom")),
    ]
    rep = service.ingest_game(s, g, "US", conns)
    assert rep["stores"]["steam"] == "ok" and rep["stores"]["epic"].startswith("error")
    outcomes = {(j.store_id, j.outcome) for j in s.query(JobRun)}
    assert outcomes == {("steam", "ok"), ("epic", "error")}


def test_rate_limit_circuit_breaker(s):
    games = [service.upsert_game(s, t, None, 2020) for t in ("Hades", "Celeste", "Portal")]
    limited = Fake("steam", exc=RateLimited("429"))
    blocked: set = set()
    for g in games:
        service.ingest_game(s, g, "US", [limited], blocked)
    assert limited.calls == 1  # stopped hitting the store after the first 429
    assert "steam" in blocked
    assert s.query(JobRun).filter_by(outcome="rate_limited").count() == 1


def test_review_queue_and_approval(s):
    g = service.upsert_game(s, "Hades", None, 2020)
    service.ingest_game(s, g, "US", [Fake("epic", [raw("epic", "Hades", 1000, release_year=1990, pid="z")])])
    c = s.query(MatchCandidate).one()
    service.approve_candidate(s, c)
    assert s.query(PriceSnapshot).count() == 1


# ---------------------------------------------------------------- queries

def test_currency_conversion(s):
    g = service.upsert_game(s, "Hades", 1145360, 2020)
    service.record_snapshot(s, g, raw("steam", "Hades", 1000, 2000, pid="1"))
    data = service.current_prices(s, g, "EUR", "US")
    p = data["prices"][0]
    assert p["price_cents"] == 900 and p["converted"] and p["discount_pct"] == 50


def test_history_stats_and_historical_low(s):
    g = service.upsert_game(s, "Hades", 1145360, 2020)
    for p in (2000, 1000, 1500):
        service.record_snapshot(s, g, raw("steam", "Hades", p, 2000, pid="1"))
    st = service.history(s, g, "USD", region="US")["stores"]["steam"]
    assert (st["low_cents"], st["high_cents"], st["avg_cents"]) == (1000, 2000, 1500)
    cur = service.current_prices(s, g, "USD", "US")
    assert cur["historical_low_cents"] == 1000 and not cur["at_historical_low"]


def test_fx_triangulation(s):
    assert fx.rate(s, "EUR", "GBP") == pytest.approx(0.75 / 0.9)
    assert fx.rate(s, "USD", "JPY") is None


def test_regions_are_isolated(monkeypatch, s):
    monkeypatch.setenv("REGIONS", "US,GB")
    g = service.upsert_game(s, "Hades", 1145360, 2020)
    service.record_snapshot(s, g, raw("steam", "Hades", 1000, 1000, pid="1", region="US"))
    service.record_snapshot(s, g, raw("steam", "Hades", 800, 800, pid="1", region="GB", currency="GBP"))
    us = service.current_prices(s, g, None, "US")
    gb = service.current_prices(s, g, None, "GB")
    assert (us["currency"], us["prices"][0]["price_cents"]) == ("USD", 1000)
    assert (gb["currency"], gb["prices"][0]["price_cents"]) == ("GBP", 800)
    with pytest.raises(KeyError):
        regions.get("JP")
    monkeypatch.setenv("REGIONS", "XX")
    with pytest.raises(ValueError):
        regions.enabled()


# ---------------------------------------------------------------- parsers

def test_store_parsers():
    st = steam.parse_search_item({"id": 620, "name": "Portal 2", "price": {"currency": "USD", "initial": 999, "final": 199}})
    assert st.discount_pct == 80 and st.steam_app_id == 620 and st.region == "US"
    assert steam.parse_search_item({"id": 1, "name": "F2P"}) is None
    e = epic.parse_element({"id": "abc", "title": "Hades", "productSlug": "hades/home", "effectiveDate": "2020-09-17T00:00:00Z",
        "price": {"totalPrice": {"discountPrice": 1249, "originalPrice": 2499, "currencyCode": "USD", "currencyInfo": {"decimals": 2}}}}, "GB")
    assert e.url.endswith("/p/hades") and e.base_price_cents == 2499 and e.region == "GB"


# ---------------------------------------------------------------- GOGDB

def make_archive(tmp_path, layout="nested"):
    """ASSUMED layout (unverified against the real dump): <id>/product.json + <id>/prices.json."""
    def add(tf, name, obj, gz=False):
        data = json.dumps(obj).encode()
        if gz:
            data = gzip.compress(data)
            name += ".gz"
        ti = tarfile.TarInfo(name)
        ti.size = len(data)
        tf.addfile(ti, io.BytesIO(data))

    path = tmp_path / "gogdb_2026-09-17.tar.xz"
    with tarfile.open(path, "w:xz") as tf:
        add(tf, "products/1207658924/product.json", {"title": "Hades", "product_type": "game", "release_date": "2020-09-17T00:00:00+0000", "slug": "hades"})
        # shape A: country -> currency -> records with cent strings
        add(tf, "products/1207658924/prices.json", {"US": {"USD": [
            {"date": "2026-01-01T00:00:00Z", "price_base": "2499", "price_final": "2499"},
            {"date": "2026-03-01T00:00:00Z", "price_base": "2499", "price_final": "1249"},
            {"date": "2026-03-10T00:00:00Z", "price_base": "2499", "price_final": "0"},   # giveaway: ignored
            {"date": "2026-04-01T00:00:00Z", "price_base": "2499", "price_final": "2499"}]},
            "DE": {"EUR": [{"date": "2026-01-01T00:00:00Z", "price_base": "2299", "price_final": "2299"}]}}, gz=True)
        add(tf, "products/1207658925/product.json", {"title": "Hades Soundtrack", "product_type": "game"})
        add(tf, "products/1207658925/prices.json", {"US": {"USD": [{"date": "2026-01-01T00:00:00Z", "price_base": "999", "price_final": "999"}]}})
        # shape B: country -> records carrying currency, epoch timestamps
        add(tf, "products/1207658926/product.json", {"title": "Celeste", "product_type": "game", "release_date": "2018-01-25"})
        add(tf, "products/1207658926/prices.json", {"US": [{"ts": 1767225600, "currency": "USD", "base": 1999, "final": 999}]})
        add(tf, "products/1207658927/product.json", {"title": "Some DLC", "product_type": "dlc"})
        add(tf, "products/1207658927/prices.json", {"US": {"USD": [{"date": "2026-01-01T00:00:00Z", "price_base": "100", "price_final": "100"}]}})
    return path


def test_gogdb_import_and_idempotence(s, tmp_path):
    hades = service.upsert_game(s, "Hades", 1145360, 2020)
    celeste = service.upsert_game(s, "Celeste", None, 2018)
    path = make_archive(tmp_path)
    rep = gogdb.import_archive(s, path)
    assert rep["matched"] == 2 and rep["files_seen"] == {"product.json": 4, "prices.json": 4}
    h = service.history(s, hades, "USD", 3650, "US")["stores"]["gog"]
    # 3 real records (giveaway skipped) + carry-forward point at the dump date
    assert len(h["points"]) == 4 and h["low_cents"] == 1249 and h["high_cents"] == 2499
    assert s.query(Listing).filter_by(region="DE").count() == 0  # only US is enabled
    assert service.current_prices(s, celeste, "USD", "US")["prices"][0]["price_cents"] == 999
    # soundtrack / DLC never matched to base games
    n = s.query(PriceSnapshot).count()
    rep2 = gogdb.import_archive(s, path)
    assert s.query(PriceSnapshot).count() == n and rep2["snapshots_added"] == 0


def test_gogdb_empty_archive_reports_error(s, tmp_path):
    p = tmp_path / "gogdb_2026-09-17.tar.xz"
    with tarfile.open(p, "w:xz") as tf:
        ti = tarfile.TarInfo("readme.txt"); ti.size = 2; tf.addfile(ti, io.BytesIO(b"hi"))
    rep = gogdb.import_archive(s, p)
    assert rep["products_with_prices"] == 0
    assert s.query(JobRun).filter_by(store_id="gog", outcome="error").count() == 1


# ---------------------------------------------------------------- API

@pytest.fixture()
def client_(monkeypatch):
    from app import main
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from app import models  # noqa
    db.Base.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng, expire_on_commit=False)

    def override():
        with Sess() as x:
            yield x
    main.app.dependency_overrides[main.get_session] = override
    with Sess() as x:
        service.seed_stores(x)
        g = service.upsert_game(x, "Portal 2", 620, 2011)
        service.record_snapshot(x, g, raw("steam", "Portal 2", 199, 999, pid="620", steam_app_id=620))
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def test_api_smoke(client_):
    c = client_
    assert c.get("/health").json() == {"status": "ok"}
    gid = c.get("/api/games", params={"q": "portal"}).json()[0]["id"]
    assert c.get(f"/api/games/{gid}/prices").json()["prices"][0]["price_cents"] == 199
    assert c.get(f"/api/games/{gid}/prices?region=JP").status_code == 400
    assert c.get("/api/games/999/prices").status_code == 404
    assert c.get(f"/api/games/{gid}/history").json()["stores"]["steam"]["low_cents"] == 199
    assert c.get("/api/regions").json()["default"] == "US"


def test_game_image_url(s, client_):
    with_id = service.game_dict(service.upsert_game(s, "Portal 2", 620, 2011))
    assert with_id["image_url"].endswith("/steam/apps/620/header.jpg")
    assert service.game_dict(service.upsert_game(s, "Epic Only Game"))["image_url"] is None
    listed = client_.get("/api/games", params={"q": "portal"}).json()[0]
    assert listed["image_url"] == with_id["image_url"]
    assert client_.get(f"/api/games/{listed['id']}/prices").json()["game"]["image_url"] == with_id["image_url"]


def test_stats_and_metrics(client_):
    st = client_.get("/api/stats").json()
    assert st["games_tracked"] == 1 and st["price_snapshots"] == 1 and st["regions"] == ["US"]
    body = client_.get("/metrics").text
    assert "playmatch_games_tracked 1.0" in body and "playmatch_http_requests_total" in body


def test_admin_requires_token(client_, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOKEN", "")
    assert client_.post("/api/admin/games", params={"title": "X"}).status_code == 403
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekret")
    assert client_.post("/api/admin/games", params={"title": "X"}).status_code == 401
    assert client_.post("/api/admin/games", params={"title": "X"}, headers={"X-Admin-Token": "nope"}).status_code == 401
    assert client_.post("/api/admin/games", params={"title": "X"}, headers={"X-Admin-Token": "sekret"}).status_code == 200
