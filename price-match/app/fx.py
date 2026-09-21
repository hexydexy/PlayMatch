"""Daily FX rates from frankfurter.app (ECB reference rates, free, no key)."""
from __future__ import annotations

from datetime import date

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config
from .models import FxRate

URL = "https://api.frankfurter.app/latest"
SUPPORTED = ["USD", "EUR", "GBP", "CAD", "AUD", "JPY", "BRL", "PLN", "SEK", "NOK", "CHF", "MXN"]


def refresh(session: Session, base: str = config.BASE_CURRENCY) -> int:
    today = date.today().isoformat()
    r = httpx.get(URL, params={"from": base}, timeout=config.HTTP_TIMEOUT)
    r.raise_for_status()
    n = 0
    for quote, rate in r.json()["rates"].items():
        exists = session.scalar(select(FxRate).where(
            FxRate.base == base, FxRate.quote == quote, FxRate.fetched_on == today))
        if not exists:
            session.add(FxRate(base=base, quote=quote, rate=rate, fetched_on=today))
            n += 1
    session.commit()
    return n


def _latest(session: Session, base: str, quote: str) -> float | None:
    row = session.scalars(select(FxRate).where(FxRate.base == base, FxRate.quote == quote)
                          .order_by(FxRate.fetched_on.desc())).first()
    return row.rate if row else None


def rate(session: Session, src: str, dst: str) -> float | None:
    """Rate to convert 1 unit of src into dst, using the latest stored rates."""
    if src == dst:
        return 1.0
    direct = _latest(session, src, dst)
    if direct:
        return direct
    inv = _latest(session, dst, src)
    if inv:
        return 1 / inv
    b = config.BASE_CURRENCY  # triangulate through the base currency
    a, c = _latest(session, b, src), _latest(session, b, dst)
    if src == b and c:
        return c
    if dst == b and a:
        return 1 / a
    if a and c:
        return c / a
    return None


def convert_cents(session: Session, cents: int, src: str, dst: str) -> int | None:
    r = rate(session, src, dst)
    return None if r is None else round(cents * r)
