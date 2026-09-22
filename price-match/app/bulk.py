"""Bulk Steam price refresh for the whole catalog.

Games are sent to Steam in batches. Never-checked games go first, then the least
recently checked, so a run cut short (rate limit, restart) resumes where it stopped.
Every game in a successful batch is marked checked, including those Steam returned no
price for (free to play, unreleased, delisted), so they are not retried forever.

    python -m app.bulk --probe     # find the largest batch size Steam fully answers
"""
from __future__ import annotations

import logging
from collections import deque

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config, service
from .connectors.base import RateLimited
from .connectors.steam import SteamConnector
from .models import Game, PriceCheck, utcnow

log = logging.getLogger("playmatch.bulk")

MAX_CONSECUTIVE_FAILURES = 10


def games_due(s: Session, region: str) -> list[tuple[int, int, str]]:
    """(game id, steam app id, title): never-checked first, then oldest check."""
    q = (select(Game.id, Game.steam_app_id, Game.title)
         .outerjoin(PriceCheck, (PriceCheck.game_id == Game.id)
                    & (PriceCheck.store_id == "steam") & (PriceCheck.region == region))
         .where(Game.steam_app_id.is_not(None))
         .order_by(PriceCheck.checked_at.asc().nulls_first(), Game.id))
    return [(gid, aid, title) for gid, aid, title in s.execute(q)]


def refresh_steam_prices(s: Session, region: str = "US", connector=None,
                         batch_size: int | None = None, blocked: set[str] | None = None) -> dict:
    connector = connector or SteamConnector()
    size = max(1, batch_size or config.STEAM_BATCH_SIZE)
    blocked = blocked if blocked is not None else set()
    rep = {"games": 0, "batches": 0, "priced": 0, "no_price": 0, "snapshots": 0,
           "errors": 0, "rate_limited": False, "aborted": False}
    if "steam" in blocked:
        rep["rate_limited"] = True
        return rep
    due = games_due(s, region)
    rep["games"] = len(due)
    pending = deque(due[i:i + size] for i in range(0, len(due), size))
    consecutive_failures = 0
    while pending:
        batch = pending.popleft()
        ids = [app_id for _, app_id, _ in batch]
        try:
            result = connector.prices_for(ids, region)
        except RateLimited as e:
            blocked.add("steam")
            rep["rate_limited"] = True
            log.error("steam rate limited us; halting Steam for this run: %s", e)
            service.log_job(s, "ingest", "steam", region, "rate_limited", str(e))
            break
        except Exception as e:
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                rep["aborted"] = True
                log.error("aborting run: %d consecutive Steam failures", MAX_CONSECUTIVE_FAILURES)
                service.log_job(s, "ingest", "steam", region, "error",
                               f"aborting run: {MAX_CONSECUTIVE_FAILURES} consecutive Steam failures")
                break
            if len(batch) > 1:  # an oversized or bad batch: retry as two halves
                mid = len(batch) // 2
                pending.appendleft(batch[mid:])
                pending.appendleft(batch[:mid])
                continue
            rep["errors"] += 1
            log.warning("steam batch of 1 failed (app %s): %s", ids[0], e)
            service.log_job(s, "ingest", "steam", region, "error", str(e))
            continue
        consecutive_failures = 0
        _record_batch(s, batch, result, region, rep)
    return rep


def _record_batch(s: Session, batch, result: dict, region: str, rep: dict) -> None:
    now = utcnow()
    games = {g.id: g for g in s.scalars(select(Game).where(Game.id.in_([gid for gid, _, _ in batch])))}
    for game_id, app_id, title in batch:
        raw = result.get(app_id)
        if raw is None:
            rep["no_price"] += 1
            continue
        raw.title, raw.region = title, region
        rep["priced"] += 1
        if service.record_snapshot_if_due(s, games[game_id], raw, now):
            rep["snapshots"] += 1
    service.mark_checked(s, [gid for gid, _, _ in batch], "steam", region, now)  # commits
    service.log_job(s, "ingest", "steam", region, "ok")
    rep["batches"] += 1


def probe_batch_size(connector, app_ids: list[int], region: str = "US",
                     sizes=(50, 100)) -> int:
    """Largest tested size for which Steam returned an entry for at least 95% of the ids
    asked for. Stops at the first size that is not fully answered. 0 if even the smallest fails."""
    best = 0
    for size in sizes:
        if size > len(app_ids):
            break
        try:
            payload = connector.fetch_batch_payload(app_ids[:size], region)
        except Exception as e:
            log.info("probe: size %d failed: %s", size, e)
            break
        if len(payload) < 0.95 * size:
            break
        best = size
    return best


def main():
    import argparse

    from .db import SessionLocal, init_db

    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="find the largest batch size Steam fully answers")
    ap.add_argument("--region", default="US")
    a = ap.parse_args()
    if not a.probe:
        ap.error("nothing to do; use --probe (the daily job runs the refresh itself)")
    init_db()
    with SessionLocal() as s:
        ids = [aid for _, aid, _ in games_due(s, a.region)][:100]
    if len(ids) < 50:
        raise SystemExit(f"need at least 50 games with a Steam id in the database (have {len(ids)}); run catalog sync first")
    best = probe_batch_size(SteamConnector(), ids, a.region)
    print(f"largest batch size fully answered: {best}" if best else "even a batch of 50 was not fully answered")
    print(f"set STEAM_BATCH_SIZE={best} in .env" if best else "keep STEAM_BATCH_SIZE at a smaller value, e.g. 20")


if __name__ == "__main__":
    main()
