"""Ingestion + query logic for price-match and price-history."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import config, fx, regions
from .connectors.base import ConnectorError, RateLimited, RawListing, polite_sleep
from .connectors.epic import EpicConnector
from .connectors.steam import SteamConnector
from .matcher import evaluate, normalize
from .models import (Game, JobRun, Listing, MatchCandidate, PriceCheck, PriceSnapshot,
                     Store, utcnow)

log = logging.getLogger("playmatch")

# GOG is fed by the GOGDB dump importer (app/gogdb.py), not a live connector.
STORES = [("steam", "Steam", "first_party"), ("gog", "GOG", "first_party"),
          ("epic", "Epic Games", "first_party")]


def default_connectors():
    return [SteamConnector(), EpicConnector()]


def seed_stores(s: Session):
    for sid, name, typ in STORES:
        if not s.get(Store, sid):
            s.add(Store(id=sid, name=name, type=typ))
    s.commit()


def log_job(s: Session, kind: str, store_id: str, region: str, outcome: str, detail: str | None = None):
    s.add(JobRun(kind=kind, store_id=store_id, region=region, outcome=outcome,
                 detail=(detail or "")[:300] or None))
    s.commit()


def as_utc(dt: datetime) -> datetime:
    """SQLite hands back naive datetimes and Postgres aware ones; treat naive as UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def mark_checked(s: Session, game_ids: list[int], store_id: str, region: str,
                 now: datetime | None = None) -> None:
    """Record that `store_id` was asked about these games just now (insert or update)."""
    if not game_ids:
        return
    now = now or utcnow()
    have = {c.game_id: c for c in s.scalars(select(PriceCheck).where(
        PriceCheck.game_id.in_(game_ids), PriceCheck.store_id == store_id,
        PriceCheck.region == region))}
    for gid in game_ids:
        if gid in have:
            have[gid].checked_at = now
        else:
            s.add(PriceCheck(game_id=gid, store_id=store_id, region=region, checked_at=now))
    s.commit()


def upsert_game(s: Session, title: str, steam_app_id: int | None = None,
                release_year: int | None = None) -> Game:
    g = None
    if steam_app_id:
        g = s.scalar(select(Game).where(Game.steam_app_id == steam_app_id))
    if not g:
        g = s.scalar(select(Game).where(Game.norm_title == normalize(title)))
    if not g:
        g = Game(title=title, norm_title=normalize(title),
                 release_year=release_year, steam_app_id=steam_app_id)
        s.add(g)
    else:
        g.release_year = g.release_year or release_year
        g.steam_app_id = g.steam_app_id or steam_app_id
    s.commit()
    return g


def get_or_create_listing(s: Session, game: Game, store_id: str, product_id: str,
                          region: str, url: str | None) -> Listing:
    listing = s.scalar(select(Listing).where(
        Listing.store_id == store_id, Listing.store_product_id == product_id,
        Listing.region == region))
    if not listing:
        listing = Listing(game_id=game.id, store_id=store_id, store_product_id=product_id,
                          url=url, region=region)
        s.add(listing)
        s.flush()
    return listing


def record_snapshot(s: Session, game: Game, raw: RawListing) -> PriceSnapshot:
    listing = get_or_create_listing(s, game, raw.store_id, raw.product_id, raw.region, raw.url)
    snap = PriceSnapshot(listing_id=listing.id, price_cents=raw.price_cents,
                         base_price_cents=raw.base_price_cents, currency=raw.currency,
                         discount_pct=raw.discount_pct)
    s.add(snap)
    s.commit()
    return snap


def ingest_game(s: Session, game: Game, region: str | None = None, connectors=None,
                blocked: set[str] | None = None) -> dict:
    """Fetch each live store for one game/region, match, and append snapshots.

    `blocked` is shared across a whole run: once a store rate-limits us it is
    skipped for every remaining game (circuit breaker), so we back off instead
    of hammering a server that has told us to stop.
    """
    region = regions.get(region).code
    connectors = connectors if connectors is not None else default_connectors()
    blocked = blocked if blocked is not None else set()
    report = {"game": game.title, "region": region, "stores": {}}
    for c in connectors:
        sid = c.store_id
        if sid in blocked:
            report["stores"][sid] = "skipped: rate limited earlier in this run"
            continue
        try:
            if sid == "steam" and game.steam_app_id and hasattr(c, "by_app_id"):
                raw_list = [x for x in [c.by_app_id(game.steam_app_id, region)] if x]
            else:
                raw_list = c.search(game.title, region)
        except RateLimited as e:
            blocked.add(sid)
            log.error("%s rate limited us; halting this store for the run: %s", sid, e)
            report["stores"][sid] = f"rate_limited: {e}"
            log_job(s, "ingest", sid, region, "rate_limited", str(e))
            continue
        except Exception as e:  # one store failing must not stop others
            log.warning("connector %s failed for %s: %s", sid, game.title, e)
            report["stores"][sid] = f"error: {e}"
            log_job(s, "ingest", sid, region, "error", str(e))
            continue
        finally:
            polite_sleep()
        best = None
        for raw in raw_list:
            res = evaluate(game, raw)
            if res.verdict == "match" and (best is None or res.score > best[1]):
                best = (raw, res.score)
            elif res.verdict == "review":
                queue_review(s, game, raw, res.score)
        if best:
            raw = best[0]
            if raw.steam_app_id and not game.steam_app_id:
                game.steam_app_id = raw.steam_app_id
            if raw.release_year and not game.release_year:
                game.release_year = raw.release_year
            record_snapshot(s, game, raw)
            report["stores"][sid] = "ok"
            log_job(s, "ingest", sid, region, "ok")
        else:
            report["stores"][sid] = "no confident match"
            log_job(s, "ingest", sid, region, "no_match")
    return report


def queue_review(s: Session, game: Game, raw: RawListing, score: float):
    exists = s.scalar(select(MatchCandidate).where(
        MatchCandidate.game_id == game.id, MatchCandidate.store_id == raw.store_id,
        MatchCandidate.store_product_id == raw.product_id))
    if exists:
        return
    s.add(MatchCandidate(game_id=game.id, store_id=raw.store_id,
                         store_product_id=raw.product_id, store_title=raw.title,
                         url=raw.url, score=score,
                         payload={"price_cents": raw.price_cents, "base_price_cents": raw.base_price_cents,
                                  "currency": raw.currency, "release_year": raw.release_year,
                                  "region": raw.region}))
    s.commit()


def approve_candidate(s: Session, cand: MatchCandidate) -> None:
    p = cand.payload
    game = s.get(Game, cand.game_id)
    raw = RawListing(store_id=cand.store_id, product_id=cand.store_product_id,
                     title=cand.store_title, url=cand.url, price_cents=p["price_cents"],
                     base_price_cents=p["base_price_cents"], currency=p["currency"],
                     release_year=p.get("release_year"), region=p.get("region", "US"))
    record_snapshot(s, game, raw)
    cand.status = "approved"
    s.commit()


# ---------------------------------------------------------------- queries

def _latest_snapshots(s: Session, game_id: int, region: str):
    sub = (select(PriceSnapshot.listing_id, func.max(PriceSnapshot.captured_at).label("m"))
           .join(Listing).where(Listing.game_id == game_id, Listing.region == region)
           .group_by(PriceSnapshot.listing_id).subquery())
    q = (select(PriceSnapshot, Listing)
         .join(Listing, Listing.id == PriceSnapshot.listing_id)
         .join(sub, (sub.c.listing_id == PriceSnapshot.listing_id) & (sub.c.m == PriceSnapshot.captured_at)))
    return s.execute(q).all()


def current_prices(s: Session, game: Game, currency: str | None = None, region: str | None = None) -> dict:
    reg = regions.get(region)
    currency = (currency or reg.currency).upper()
    rows = []
    for snap, listing in _latest_snapshots(s, game.id, reg.code):
        price = fx.convert_cents(s, snap.price_cents, snap.currency, currency)
        base = fx.convert_cents(s, snap.base_price_cents, snap.currency, currency)
        store = s.get(Store, listing.store_id)
        rows.append({
            "store_id": store.id, "store": store.name, "store_type": store.type,
            "label": "Official store price",
            "url": listing.url, "region": listing.region,
            "price_cents": price, "base_price_cents": base, "currency": currency,
            "native_price_cents": snap.price_cents, "native_currency": snap.currency,
            "discount_pct": snap.discount_pct,
            "captured_at": snap.captured_at.isoformat(),
            "converted": snap.currency != currency,
        })
    priced = [r for r in rows if r["price_cents"] is not None]
    lowest = min(priced, key=lambda r: r["price_cents"], default=None)
    for r in rows:
        r["is_lowest"] = bool(lowest and r is lowest)
    hist = history(s, game, currency, days=3650, region=reg.code)
    hist_low = min((p["price_cents"] for st in hist["stores"].values() for p in st["points"]), default=None)
    return {
        "game": game_dict(game), "region": reg.code, "currency": currency,
        "prices": sorted(rows, key=lambda r: (r["price_cents"] is None, r["price_cents"] or 0)),
        "lowest_now_cents": lowest["price_cents"] if lowest else None,
        "historical_low_cents": hist_low,
        "at_historical_low": bool(lowest and hist_low is not None and lowest["price_cents"] <= hist_low),
    }


def history(s: Session, game: Game, currency: str | None = None, days: int = 365,
            region: str | None = None) -> dict:
    reg = regions.get(region)
    currency = (currency or reg.currency).upper()
    since = utcnow() - timedelta(days=days)
    q = (select(PriceSnapshot, Listing).join(Listing).where(
        Listing.game_id == game.id, Listing.region == reg.code,
        PriceSnapshot.captured_at >= since).order_by(PriceSnapshot.captured_at))
    stores: dict[str, dict] = {}
    for snap, listing in s.execute(q).all():
        price = fx.convert_cents(s, snap.price_cents, snap.currency, currency)
        if price is None:
            continue
        st = stores.setdefault(listing.store_id, {"points": []})
        st["points"].append({"t": snap.captured_at.isoformat(), "price_cents": price,
                             "discount_pct": snap.discount_pct})
    for st in stores.values():
        prices = [p["price_cents"] for p in st["points"]]
        st["low_cents"] = min(prices)
        st["high_cents"] = max(prices)
        st["avg_cents"] = round(sum(prices) / len(prices))
    return {"game_id": game.id, "region": reg.code, "currency": currency, "days": days, "stores": stores}


STEAM_HEADER_URL = "https://cdn.cloudflare.steamstatic.com/steam/apps/{}/header.jpg"


def game_dict(g: Game) -> dict:
    # Cover art comes from Steam's CDN; games without a Steam id have none and the UI draws a fallback.
    image = STEAM_HEADER_URL.format(g.steam_app_id) if g.steam_app_id else None
    return {"id": g.id, "title": g.title, "release_year": g.release_year,
            "steam_app_id": g.steam_app_id, "image_url": image}


def stats(s: Session) -> dict:
    """Headline numbers for the public stats endpoint and Prometheus gauges."""
    week_ago = utcnow() - timedelta(days=7)
    per_store = {}
    for sid, cnt in s.execute(select(Listing.store_id, func.count()).group_by(Listing.store_id)):
        per_store[sid] = {"listings": cnt}
    for sid, outcome, cnt in s.execute(
            select(JobRun.store_id, JobRun.outcome, func.count())
            .where(JobRun.at >= week_ago).group_by(JobRun.store_id, JobRun.outcome)):
        d = per_store.setdefault(sid, {"listings": 0}).setdefault("runs_7d", {})
        d[outcome] = cnt
    for d in per_store.values():
        runs = d.get("runs_7d", {})
        total = sum(runs.values())
        d["success_rate_7d"] = round(runs.get("ok", 0) / total, 3) if total else None
    first, last, n = s.execute(select(func.min(PriceSnapshot.captured_at),
                                      func.max(PriceSnapshot.captured_at),
                                      func.count(PriceSnapshot.id))).one()
    return {
        "games_tracked": s.scalar(select(func.count(Game.id))),
        "listings": s.scalar(select(func.count(Listing.id))),
        "price_snapshots": n,
        "first_snapshot": first.isoformat() if first else None,
        "last_snapshot": last.isoformat() if last else None,
        "pending_reviews": s.scalar(select(func.count(MatchCandidate.id)).where(MatchCandidate.status == "pending")),
        "regions": [r.code for r in regions.enabled()],
        "stores": per_store,
    }
