"""Daily data job: catalog sync, FX refresh, bulk Steam prices, Epic for seed games, GOGDB import.

    python -m app.snapshot                # everything
    python -m app.snapshot --seed         # load seed_games.json first
    python -m app.snapshot --skip-catalog # do not list Steam games (no API key needed)
    python -m app.snapshot --skip-gogdb   # live stores only
    python -m app.snapshot --skip-live    # GOGDB only
"""
import argparse
import json
import logging
import pathlib

from sqlalchemy import select

from . import bulk, catalog, config, fx, gogdb, regions, service
from .connectors.epic import EpicConnector
from .db import SessionLocal, init_db
from .models import Game

SEED = pathlib.Path(__file__).with_name("seed_games.json")
log = logging.getLogger("playmatch.job")


def curated_app_ids() -> set[int]:
    """Steam ids of the seed games: the only games whose Epic price the daily job refreshes."""
    return {g["steam_app_id"] for g in json.loads(SEED.read_text()) if g.get("steam_app_id")}


def _step(s, name, fn, *args, store_id=None, kind="daily_job", **kwargs):
    """Run one job step so that a failure cannot stop the rest of the run.

    On any exception: log it, roll the shared session back (a failed flush leaves it unusable),
    and, if `store_id` is given, record an error JobRun. Returns None then, else fn's result.
    """
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        log.exception("%s failed: %s", name, e)
        try:
            s.rollback()
            if store_id:
                service.log_job(s, kind, store_id, "-", "error", str(e))
        except Exception:
            log.exception("could not record the %s failure", name)
        return None


def _refresh_epic(s, g, region, blocked) -> None:
    report = service.ingest_game(s, g, region, connectors=[EpicConnector()], blocked=blocked)
    print(report)
    if report["stores"].get("epic") in ("ok", "no confident match"):
        service.mark_checked(s, [g.id], "epic", region)


def run(seed=False, live=True, gog=True, catalog_sync=True) -> None:
    init_db()
    with SessionLocal() as s:
        service.seed_stores(s)
        if seed:
            for g in json.loads(SEED.read_text()):
                service.upsert_game(s, g["title"], g.get("steam_app_id"), g.get("year"))
        if catalog_sync:
            print(_step(s, "catalog sync", catalog.sync, s, store_id="steam"))
        try:
            fx.refresh(s)
        except Exception as e:
            log.warning("fx refresh failed, using stored rates: %s", e)
            s.rollback()
        if live:
            blocked: set[str] = set()  # shared: a rate-limited store is skipped for the rest of the run
            curated = _step(s, "seed game list", curated_app_ids, store_id="epic") or set()
            for region in regions.enabled():
                print(_step(s, "steam bulk prices", bulk.refresh_steam_prices, s, region.code,
                            blocked=blocked, store_id="steam"))
                for g in s.scalars(select(Game).where(Game.steam_app_id.in_(curated))).all():
                    _step(s, "epic refresh", _refresh_epic, s, g, region.code, blocked, store_id="epic")
        if gog:
            _step(s, "gogdb import", lambda: print(gogdb.import_archive(
                s, gogdb.download_latest(), review_cap=config.REVIEW_QUEUE_CAP_PER_RUN)),
                store_id="gog", kind="gogdb_import")


def main():
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--skip-catalog", action="store_true")
    ap.add_argument("--skip-live", action="store_true")
    ap.add_argument("--skip-gogdb", action="store_true")
    a = ap.parse_args()
    run(a.seed, not a.skip_live, not a.skip_gogdb, not a.skip_catalog)


if __name__ == "__main__":
    main()
