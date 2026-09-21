from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from .. import config

# Words that mark a listing as something other than the base game.
NON_BASE_MARKERS = (
    "dlc", "soundtrack", "artbook", "season pass", "expansion", "bundle",
    "deluxe", "ultimate", "gold edition", "complete edition", "collection",
    "pack", "upgrade", "demo", "playtest", "pre-order",
)


@dataclass
class RawListing:
    store_id: str
    product_id: str
    title: str
    url: str | None
    price_cents: int
    base_price_cents: int
    currency: str
    region: str = "US"
    release_year: int | None = None
    steam_app_id: int | None = None
    extra: dict = field(default_factory=dict)

    @property
    def discount_pct(self) -> int:
        if self.base_price_cents <= 0:
            return 0
        return max(0, round(100 * (1 - self.price_cents / self.base_price_cents)))


class ConnectorError(Exception):
    pass


class RateLimited(ConnectorError):
    """The store asked us to slow down. The caller must stop hitting this store
    for the rest of the run so we never get the server's IP banned."""


class Connector(Protocol):
    store_id: str

    def search(self, title: str, region: str) -> list[RawListing]: ...


def client() -> httpx.Client:
    return httpx.Client(
        timeout=config.HTTP_TIMEOUT,
        headers={"User-Agent": config.USER_AGENT},
        follow_redirects=True,
    )


def polite_sleep():
    if config.REQUEST_DELAY:
        time.sleep(config.REQUEST_DELAY)


def check_status(r: httpx.Response, store: str, limited=(429,)):
    if r.status_code in limited:
        raise RateLimited(f"{store} rate limited (HTTP {r.status_code}, retry-after={r.headers.get('retry-after')})")
    if r.status_code != 200:
        raise ConnectorError(f"{store} HTTP {r.status_code}")


def to_cents(value) -> int:
    return int(round(float(value) * 100))
