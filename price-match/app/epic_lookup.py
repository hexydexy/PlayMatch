"""Epic price lookup for one game, triggered when someone opens it.

Epic has no bulk method here, so non-seed games are looked up on demand. Three limits
protect Epic (and us): a per-game cooldown (a `price_checks` row), a global token bucket,
and a cool-off after Epic rate limits us.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta

from sqlalchemy.orm import Session

from . import config, service
from .connectors.epic import EpicConnector
from .models import Game, PriceCheck, utcnow

log = logging.getLogger("playmatch.epic")
BLOCK_SECONDS = 15 * 60


class Gate:
    """Global limits for on-demand Epic lookups. Per process, so it holds for one API worker."""

    def __init__(self, per_minute: int | None = None, clock=time.monotonic):
        self.per_minute = config.EPIC_ON_DEMAND_PER_MINUTE if per_minute is None else per_minute
        self.clock = clock
        self.tokens = float(self.per_minute)
        self.last = clock()
        self.blocked_until = 0.0

    def blocked(self) -> bool:
        return self.clock() < self.blocked_until

    def block(self) -> None:
        self.blocked_until = self.clock() + BLOCK_SECONDS

    def allow(self) -> bool:
        now = self.clock()
        self.tokens = min(float(self.per_minute), self.tokens + (now - self.last) * self.per_minute / 60)
        self.last = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


default_gate = Gate()


def _iso(row: PriceCheck | None) -> str | None:
    return service.as_utc(row.checked_at).isoformat() if row else None


def check_epic(s: Session, game: Game, region: str, connector=None, gate: Gate | None = None,
               now=None) -> dict:
    gate = gate or default_gate
    now = now or utcnow()
    row = s.get(PriceCheck, (game.id, "epic", region))
    if row and now - service.as_utc(row.checked_at) < timedelta(hours=config.EPIC_COOLDOWN_HOURS):
        return {"status": "fresh", "checked_at": _iso(row)}
    if gate.blocked():
        return {"status": "unavailable", "checked_at": _iso(row)}
    if not gate.allow():
        return {"status": "busy", "checked_at": _iso(row)}
    report = service.ingest_game(s, game, region, connectors=[connector or EpicConnector()], blocked=set())
    outcome = report["stores"].get("epic", "")
    if outcome.startswith("rate_limited"):
        gate.block()
        return {"status": "unavailable", "checked_at": _iso(row)}
    if outcome.startswith("error"):
        return {"status": "unavailable", "checked_at": _iso(row)}
    service.mark_checked(s, [game.id], "epic", region, now)
    return {"status": "checked", "checked_at": service.as_utc(now).isoformat()}
