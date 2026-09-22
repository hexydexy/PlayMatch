"""Demo data for the browser checks. Run from price-match/ with DATABASE_URL set:

    python -m scripts.seed_demo

Creates 120 filler games plus four named ones (124 in total):
  Hades              Steam price, Steam and Epic both checked just now
  Fresh Steam Only   Steam price, Steam checked now, Epic never checked
  Old Price Game     Steam price last checked 30 days ago, Epic checked now
  Free Game          no prices, nothing checked
"""
from datetime import timedelta

from app import service
from app.connectors.base import RawListing
from app.db import SessionLocal, init_db
from app.models import utcnow
from scripts import scale_test


def steam(app_id, title, price):
    return RawListing("steam", str(app_id), title, f"https://store.steampowered.com/app/{app_id}",
                      price, price + 1000, "USD", steam_app_id=app_id)


def main():
    init_db()
    scale_test.populate(120)
    now = utcnow()
    with SessionLocal() as s:
        service.seed_stores(s)
        hades = service.upsert_game(s, "Hades", 1145360, 2020)
        service.record_snapshot_if_due(s, hades, steam(1145360, "Hades", 1499), now)
        s.commit()
        service.mark_checked(s, [hades.id], "steam", "US", now)
        service.mark_checked(s, [hades.id], "epic", "US", now)

        fresh = service.upsert_game(s, "Fresh Steam Only", 8000001, 2024)
        service.record_snapshot_if_due(s, fresh, steam(8000001, "Fresh Steam Only", 999), now)
        s.commit()
        service.mark_checked(s, [fresh.id], "steam", "US", now)

        old = service.upsert_game(s, "Old Price Game", 8000002, 2019)
        long_ago = now - timedelta(days=30)
        service.record_snapshot_if_due(s, old, steam(8000002, "Old Price Game", 499), long_ago)
        s.commit()
        service.mark_checked(s, [old.id], "steam", "US", long_ago)
        service.mark_checked(s, [old.id], "epic", "US", now)

        service.upsert_game(s, "Free Game", 8000003, 2023)
    print("demo data ready")


if __name__ == "__main__":
    main()
