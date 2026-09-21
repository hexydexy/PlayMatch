"""Region model. v1 enables only US; adding a region is a config change.

Set REGIONS=US,GB,DE to enable more. The first entry is the default region.
Each region maps to the storefront country code used by connectors and the
currency its prices are quoted in. Listings, snapshots and the API are all
keyed by region already, so no schema change is needed to add one.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Region:
    code: str       # ISO 3166-1 alpha-2, also the storefront country code
    currency: str   # store's native currency for this region
    name: str


KNOWN = {r.code: r for r in [
    Region("US", "USD", "United States"),
    Region("GB", "GBP", "United Kingdom"),
    Region("DE", "EUR", "Germany"),
    Region("FR", "EUR", "France"),
    Region("CA", "CAD", "Canada"),
    Region("AU", "AUD", "Australia"),
    Region("BR", "BRL", "Brazil"),
    Region("PL", "PLN", "Poland"),
]}


def enabled() -> list[Region]:
    codes = [c.strip().upper() for c in os.getenv("REGIONS", "US").split(",") if c.strip()]
    unknown = [c for c in codes if c not in KNOWN]
    if unknown:
        raise ValueError(f"Unknown region(s) in REGIONS: {unknown}. Known: {sorted(KNOWN)}")
    return [KNOWN[c] for c in codes]


def default() -> Region:
    return enabled()[0]


def get(code: str | None) -> Region:
    code = (code or default().code).upper()
    for r in enabled():
        if r.code == code:
            return r
    raise KeyError(code)
