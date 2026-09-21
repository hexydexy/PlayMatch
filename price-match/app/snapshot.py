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


def run(seed=False, live=True, gog=True, catalog_sync=True) -> None:
    init_db()
    with SessionLocal() as s:
        service.seed_stores(s)
        if seed:
            for g in json.loads(SEED.read_text()):
                service.upsert_game(s, g["title"], g.get("steam_app_id"), g.get("year"))
        if catalog_sync:
            print(catalog.sync(s))
        try:
            fx.refresh(s)
        except Exception as e:
            log.warning("fx refresh failed, using stored rates: %s", e)
        if live:
            blocked: set[str] = set()  # shared: a rate-limited store is skipped for the rest of the run
            curated = curated_app_ids()
            for region in regions.enabled():
                print(bulk.refresh_steam_prices(s, region.code, blocked=blocked))
                for g in s.scalars(select(Game).where(Game.steam_app_id.in_(curated))).all():
                    report = service.ingest_game(s, g, region.code, connectors=[EpicConnector()],
                                                 blocked=blocked)
                    print(report)
                    if report["stores"].get("epic") in ("ok", "no confident match"):
                        service.mark_checked(s, [g.id], "epic", region.code)
        if gog:
            try:
                print(gogdb.import_archive(s, gogdb.download_latest(),
                                           review_cap=config.REVIEW_QUEUE_CAP_PER_RUN))
            except Exception as e:
                log.error("GOGDB import failed: %s", e)
                service.log_job(s, "gogdb_import", "gog", "-", "error", str(e))


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
