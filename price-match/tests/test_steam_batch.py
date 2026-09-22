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


def test_prices_for_treats_non_json_200_body_as_error(monkeypatch):
    monkeypatch.setattr(steam, "client", _mock(lambda req: httpx.Response(200, content=b"<html>")))
    with pytest.raises(ConnectorError):
        steam.SteamConnector().prices_for([1], "US")


def test_parse_price_batch_tolerates_malformed_entries():
    payload = {
        "1": PRICED,
        "2": {"success": True, "data": {"price_overview": {"currency": "USD", "initial": 5999}}},  # missing final
        "3": {"success": True, "data": {"price_overview": {"currency": "USD", "initial": 5999, "final": None}}},  # null final
        "4": {"success": True, "data": []},  # data is a list
        "5": {"success": True, "data": "invalid"},  # data is a string
    }
    out = steam.parse_price_batch(payload, [1, 2, 3, 4, 5])
    assert set(out) == {1, 2, 3, 4, 5}
    assert out[1] is not None and out[1].price_cents == 2999  # id 1 valid
    assert out[2] is None and out[3] is None and out[4] is None and out[5] is None  # ids 2-5 malformed
