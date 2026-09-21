"""Epic Games connector via the unofficial storefront GraphQL endpoint.

Epic has no stable public API; this may break without notice. Errors are
surfaced as ConnectorError so one broken store never blocks the others.
"""
from __future__ import annotations

import re

from .. import regions
from .base import ConnectorError, RawListing, check_status, client

GRAPHQL = "https://store.epicgames.com/graphql"
QUERY = """
query searchStoreQuery($keywords: String!, $country: String!, $locale: String) {
  Catalog {
    searchStore(keywords: $keywords, country: $country, locale: $locale, count: 5,
                category: "games/edition/base", allowCountries: $country) {
      elements {
        id title productSlug urlSlug effectiveDate
        catalogNs { mappings(pageType: "productHome") { pageSlug } }
        price(country: $country) {
          totalPrice { discountPrice originalPrice currencyCode
                       currencyInfo { decimals } }
        }
      }
    }
  }
}
"""


def parse_element(e: dict, region: str = "US") -> RawListing | None:
    tp = ((e.get("price") or {}).get("totalPrice")) or {}
    if not tp or tp.get("originalPrice") in (None, 0) and tp.get("discountPrice") in (None, 0):
        return None  # free or unpriced
    decimals = (tp.get("currencyInfo") or {}).get("decimals", 2)
    scale = 10 ** (decimals - 2)  # normalize to cents
    slug = e.get("productSlug") or e.get("urlSlug")
    if not slug:
        maps = ((e.get("catalogNs") or {}).get("mappings")) or []
        slug = maps[0]["pageSlug"] if maps else None
    year = None
    if e.get("effectiveDate"):
        m = re.match(r"(\d{4})", e["effectiveDate"])
        year = int(m.group(1)) if m else None
    return RawListing(
        store_id="epic",
        product_id=e["id"],
        title=e["title"],
        url=f"https://store.epicgames.com/p/{slug.split('/')[0]}" if slug else None,
        price_cents=int(tp["discountPrice"] / scale),
        base_price_cents=int(tp["originalPrice"] / scale),
        currency=tp["currencyCode"],
        release_year=year,
        region=region,
    )


class EpicConnector:
    store_id = "epic"

    def search(self, title: str, region: str = "US") -> list[RawListing]:
        country = regions.get(region).code
        with client() as c:
            r = c.post(GRAPHQL, json={
                "query": QUERY,
                "variables": {"keywords": title, "country": country, "locale": "en-US"},
            })
            check_status(r, "epic", (429, 403))
            body = r.json()
            if body.get("errors"):
                raise ConnectorError(f"epic graphql errors: {body['errors'][0].get('message')}")
            elements = body["data"]["Catalog"]["searchStore"]["elements"]
            return [x for x in (parse_element(e, region) for e in elements) if x]
