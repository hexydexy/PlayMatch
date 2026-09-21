"""Scale checks for the full-catalog design. Run from price-match/.

    DATABASE_URL=sqlite:///scale.db python -m scripts.scale_test populate --games 100000
    DATABASE_URL=... python -m scripts.scale_test search
    python -m scripts.scale_test match --games 100000 --pool 12000 --sample 3000

`search` times the exact query the API runs. `match` times the GOG fuzzy-matching call on a
sample of games and projects the time for the full catalog, and (if numpy is installed) also
times a chunked `cdist` alternative.
"""
import argparse
import random
import time

WORDS = ("dragon knight star war city night blade lost world last hero ghost quest rogue "
         "farm space craft dark soul witch royal iron sky sea fire ice storm shadow legend "
         "tower island road ship train zombie robot kingdom empire arena racing puzzle hunter").split()


def fake_title(rng):
    return " ".join(rng.choice(WORDS) for _ in range(rng.randint(2, 4)))


def populate(n):
    from app.db import SessionLocal, init_db
    from app.matcher import normalize
    from app.models import Game
    init_db()
    rng = random.Random(1)
    with SessionLocal() as s:
        have = s.query(Game).count()
        for start in range(0, n, 10000):
            rows = []
            for i in range(start, min(n, start + 10000)):
                t = fake_title(rng)
                rows.append({"title": t.title(), "norm_title": normalize(t), "steam_app_id": 9_000_000 + have + i})
            s.bulk_insert_mappings(Game, rows)
            s.commit()
        print(f"games in database: {s.query(Game).count()}")


def search():
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models import Game
    with SessionLocal() as s:
        for q in ("dragon", "dark soul", "zzzz", "a"):
            t0 = time.perf_counter()
            rows = s.scalars(select(Game).where(Game.norm_title.contains(q)).order_by(Game.title, Game.id).limit(48)).all()
            ms = (time.perf_counter() - t0) * 1000
            print(f"search {q!r:12} {len(rows):3} rows  {ms:8.1f} ms  {'SLOW (over 200 ms)' if ms > 200 else 'ok'}")


def match(games, pool, sample):
    from rapidfuzz import fuzz, process
    rng = random.Random(2)
    game_norms = [fake_title(rng) for _ in range(sample)]
    pool_norms = [fake_title(rng) for _ in range(pool)]
    t0 = time.perf_counter()
    for g in game_norms:
        process.extract(g, pool_norms, scorer=fuzz.ratio, limit=5, score_cutoff=85)
    per_game = (time.perf_counter() - t0) / sample
    proj = per_game * games
    print(f"extract loop: {per_game * 1000:.2f} ms/game -> {proj:.0f} s projected for {games} games "
          f"{'OVER 600 s: use chunked cdist' if proj > 600 else 'ok'}")
    try:
        import numpy as np
    except ImportError:
        print("numpy not installed; skipping the cdist comparison")
        return
    t0 = time.perf_counter()
    for i in range(0, sample, 500):
        process.cdist(game_norms[i:i + 500], pool_norms, scorer=fuzz.ratio, score_cutoff=85,
                      dtype=np.uint8, workers=-1)
    per_game = (time.perf_counter() - t0) / sample
    print(f"chunked cdist: {per_game * 1000:.2f} ms/game -> {per_game * games:.0f} s projected")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["populate", "search", "match"])
    ap.add_argument("--games", type=int, default=100000)
    ap.add_argument("--pool", type=int, default=12000)
    ap.add_argument("--sample", type=int, default=3000)
    a = ap.parse_args()
    {"populate": lambda: populate(a.games), "search": search,
     "match": lambda: match(a.games, a.pool, min(a.sample, a.games))}[a.cmd]()
