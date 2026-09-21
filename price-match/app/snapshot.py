"""Daily data job: FX refresh, live Steam/Epic snapshots, GOGDB import.

    python -m app.snapshot                # everything
    python -m app.snapshot --seed         # load seed_games.json first
    python -m app.snapshot --skip-gogdb   # live stores only
    python -m app.snapshot --skip-live    # GOGDB only
"""
import argparse
import json
import logging
import pathlib

from sqlalchemy import select

from . import fx, gogdb, regions, service
from .db import SessionLocal, init_db
from .models import Game

SEED = pathlib.Path(__file__).with_name("seed_games.json")
log = logging.getLogger("playmatch.job")


def run(seed=False, live=True, gog=True) -> None:
    init_db()
    with SessionLocal() as s:
        service.seed_stores(s)
        if seed:
            for g in json.loads(SEED.read_text()):
                service.upsert_game(s, g["title"], g.get("steam_app_id"), g.get("year"))
        try:
            fx.refresh(s)
        except Exception as e:
            log.warning("fx refresh failed, using stored rates: %s", e)
        if live:
            blocked: set[str] = set()  # shared: a rate-limited store is skipped for the rest of the run
            for region in regions.enabled():
                for g in s.scalars(select(Game)).all():
                    print(service.ingest_game(s, g, region.code, blocked=blocked))
        if gog:
            try:
                print(gogdb.import_archive(s, gogdb.download_latest()))
            except Exception as e:
                log.error("GOGDB import failed: %s", e)
                service.log_job(s, "gogdb_import", "gog", "-", "error", str(e))


def main():
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--skip-live", action="store_true")
    ap.add_argument("--skip-gogdb", action="store_true")
    a = ap.parse_args()
    run(a.seed, not a.skip_live, not a.skip_gogdb)


if __name__ == "__main__":
    main()
