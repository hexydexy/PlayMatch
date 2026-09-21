"""Steam connector using the public (unofficial) storefront endpoints.

- search:  store.steampowered.com/api/storesearch/?term=..&cc=US   (returns price too)
- details: store.steampowered.com/api/appdetails?appids=..&cc=US   (release date, price_overview)
"""
from __future__ import annotations

import re

from .. import regions
from .base import RawListing, check_status, client, polite_sleep

SEARCH = "https://store.steampowered.com/api/storesearch/"
DETAILS = "https://store.steampowered.com/api/appdetails"


def _year(text: str | None) -> int | None:
    m = re.search(r"(19|20)\d{2}", text or "")
    return int(m.group(0)) if m else None


def parse_search_item(item: dict, region: str = "US") -> RawListing | None:
    price = item.get("price")
    if not price:  # free-to-play or unreleased
        return None
    app_id = item["id"]
    return RawListing(
        store_id="steam",
        product_id=str(app_id),
        title=item["name"],
        url=f"https://store.steampowered.com/app/{app_id}",
        price_cents=int(price["final"]),
        base_price_cents=int(price.get("initial", price["final"])),
        currency=price.get("currency", "USD"),
        steam_app_id=app_id,
        region=region,
    )


def parse_details(app_id: int, payload: dict, region: str = "US") -> RawListing | None:
    entry = payload.get(str(app_id), {})
    if not entry.get("success"):
        return None
    data = entry["data"]
    po = data.get("price_overview")
    if not po:
        return None
    return RawListing(
        store_id="steam",
        product_id=str(app_id),
        title=data.get("name", ""),
        url=f"https://store.steampowered.com/app/{app_id}",
        price_cents=int(po["final"]),
        base_price_cents=int(po["initial"]),
        currency=po["currency"],
        release_year=_year((data.get("release_date") or {}).get("date")),
        steam_app_id=app_id,
        region=region,
    )


# Steam signals throttling with 429 and, for appdetails, sometimes 403.
LIMITED = (429, 403)


class SteamConnector:
    store_id = "steam"

    def search(self, title: str, region: str = "US") -> list[RawListing]:
        with client() as c:
            r = c.get(SEARCH, params={"term": title, "cc": regions.get(region).code, "l": "english"})
            check_status(r, "steam", LIMITED)
            out = []
            for item in r.json().get("items", [])[:5]:
                if item.get("type") not in (None, "app"):
                    continue
                raw = parse_search_item(item, region)
                if raw:
                    out.append(raw)
            return out

    def by_app_id(self, app_id: int, region: str = "US") -> RawListing | None:
        with client() as c:
            r = c.get(DETAILS, params={"appids": app_id, "cc": regions.get(region).code,
                                       "filters": "basic,price_overview,release_date"})
            polite_sleep()
            check_status(r, "steam", LIMITED)
            return parse_details(app_id, r.json(), region)
