"""GOG data via GOGDB's published daily dump (no scraping of GOG itself).

GOGDB (gogdb.org) publishes ~60 MB daily archives at
    <GOGDB_BASE_URL>/YYYY-MM/gogdb_YYYY-MM-DD.tar.xz
and asks people to use those instead of many small requests. We download ONE
archive per day, stream it, match products to tracked games, and backfill the
recorded price history for each enabled region.

NOTE: the archive's internal layout was not verifiable from the build sandbox.
The parser therefore looks for `<numeric id>/product.json` and
`<numeric id>/prices.json` anywhere in the tree and accepts several plausible
price-record shapes. If an import reports 0 prices, run
    python -m app.gogdb --inspect <archive>
and check the layout (see README).

Usage:
    python -m app.gogdb                      # download latest + import
    python -m app.gogdb --file X.tar.xz      # import a local archive
    python -m app.gogdb --inspect X.tar.xz   # print structure and samples
"""
from __future__ import annotations

import argparse
import gzip
import json
import logging
import re
import tarfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from rapidfuzz import fuzz, process
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config, regions
from .connectors.base import RawListing, client
from .matcher import evaluate
from .models import Game, Listing, MatchCandidate, PriceSnapshot
from .service import (get_or_create_listing, log_job, queue_review)

log = logging.getLogger("playmatch.gogdb")

MEMBER_RE = re.compile(r"(?:^|/)(\d+)/(product|prices)\.json(\.gz)?$")
DATE_IN_NAME = re.compile(r"gogdb_(\d{4})-(\d{2})-(\d{2})")
COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
BASE_KEYS = ("price_base", "base", "base_price", "price_initial", "initial")
FINAL_KEYS = ("price_final", "final", "final_price", "price_current", "current", "price")
DATE_KEYS = ("date", "datetime", "timestamp", "time", "ts", "t", "at")


# ------------------------------------------------------------- parsing

def _cents(v) -> int | None:
    """Integers and digit-strings are treated as cents; decimals as major units."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return round(v * 100)
    if isinstance(v, str):
        v = v.strip()
        if re.fullmatch(r"\d+", v):
            return int(v)
        if re.fullmatch(r"\d+[.,]\d+", v):
            return round(float(v.replace(",", ".")) * 100)
    return None


def _dt(v) -> datetime | None:
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=timezone.utc)
        if isinstance(v, str):
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return None


def _first(d: dict, keys):
    for k in keys:
        if k in d:
            return d[k]
    return None


def _as_record(node):
    """Return (dt, currency, base_cents, final_cents) if node looks like a price record."""
    if isinstance(node, dict):
        final = _cents(_first(node, FINAL_KEYS))
        when = _dt(_first(node, DATE_KEYS))
        if final is None or when is None:
            return None
        base = _cents(_first(node, BASE_KEYS))
        return when, node.get("currency"), (base if base else final), final
    if isinstance(node, list) and len(node) >= 3 and _dt(node[0]) and _cents(node[1]) is not None:
        return _dt(node[0]), None, _cents(node[1]), _cents(node[2])
    return None


def extract_prices(obj, wanted: dict[str, str]) -> dict[str, list[tuple]]:
    """Walk an arbitrary prices.json into {region: [(dt, currency, base, final)]}.

    `wanted` maps region code -> currency. Country keys are 2-letter codes,
    currency keys 3-letter codes; other keys are transparent.
    """
    out: dict[str, list[tuple]] = defaultdict(list)

    def visit(node, country, currency):
        rec = _as_record(node)
        if rec:
            when, cur, base, final = rec
            cur = cur or currency
            region = country or next((r for r, c in wanted.items() if c == cur), None)
            if region in wanted and (cur is None or cur == wanted[region]) and final > 0:
                out[region].append((when, cur or wanted[region], base, final))
            return
        if isinstance(node, dict):
            for k, v in node.items():
                if COUNTRY_RE.match(str(k)) and country is None:
                    visit(v, str(k), currency)
                elif CURRENCY_RE.match(str(k)) and currency is None:
                    visit(v, country, str(k))
                else:
                    visit(v, country, currency)
        elif isinstance(node, list):
            for v in node:
                visit(v, country, currency)

    visit(obj, None, None)
    for recs in out.values():
        recs.sort(key=lambda r: r[0])
    return out


def parse_product(d: dict, pid: str) -> dict | None:
    title = d.get("title") or d.get("name")
    if not title:
        return None
    ptype = str(d.get("product_type") or d.get("game_type") or d.get("type") or "game").lower()
    year = None
    m = re.match(r"(\d{4})", str(d.get("release_date") or d.get("globalReleaseDate") or ""))
    if m and m.group(1) != "1970":
        year = int(m.group(1))
    slug = d.get("slug")
    return {"id": pid, "title": title, "type": ptype, "year": year,
            "url": f"https://www.gog.com/game/{slug}" if slug else None}


# ------------------------------------------------------------- archive

def _read(tf, member, gz: bool):
    data = tf.extractfile(member).read()
    return json.loads(gzip.decompress(data) if gz else data)


def scan_archive(path: Path, wanted: dict[str, str]):
    products: dict[str, dict] = {}
    prices: dict[str, dict[str, list]] = {}
    seen = {"product.json": 0, "prices.json": 0}
    with tarfile.open(path, "r|*") as tf:  # streaming: the archive is never fully in memory
        for m in tf:
            if not m.isfile():
                continue
            hit = MEMBER_RE.search(m.name)
            if not hit:
                continue
            pid, kind, gz = hit.group(1), hit.group(2), bool(hit.group(3))
            try:
                obj = _read(tf, m, gz)
            except (ValueError, OSError, EOFError):
                continue
            seen[kind + ".json"] += 1
            if kind == "product":
                p = parse_product(obj, pid) if isinstance(obj, dict) else None
                if p:
                    products[pid] = p
            else:
                got = extract_prices(obj, wanted)
                if got:
                    prices[pid] = got
    return products, prices, seen


def archive_date(path: Path) -> datetime:
    m = DATE_IN_NAME.search(path.name)
    if m:
        return datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=timezone.utc)
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def _naive(d: datetime) -> datetime:
    """UTC, tz-stripped. Naive inputs (SQLite drops tzinfo) are assumed to already be UTC."""
    if d.tzinfo is None:
        return d
    return d.astimezone(timezone.utc).replace(tzinfo=None)


def import_archive(s: Session, path: Path, region_list=None, review_cap: int | None = None) -> dict:
    region_list = region_list or regions.enabled()
    wanted = {r.code: r.currency for r in region_list}
    products, prices, seen = scan_archive(path, wanted)
    stamp = archive_date(path)
    report = {"archive": path.name, "products": len(products), "products_with_prices": len(prices),
              "files_seen": seen, "matched": 0, "snapshots_added": 0, "review_queued": 0,
              "review_dropped": 0}
    if not prices:
        log_job(s, "gogdb_import", "gog", ",".join(wanted), "error",
                f"no prices parsed (saw {seen}); run --inspect")
        return report

    # candidate pool: base games that have a price in an enabled region
    pool = {pid: p for pid, p in products.items() if pid in prices and p["type"] in ("game", "")}
    pids = list(pool)
    from .matcher import normalize
    norm = [normalize(pool[p]["title"]) for p in pids]

    reviews = []  # (score, game, raw): queued after the loop, best first, up to review_cap
    for game in s.scalars(select(Game)).all():
        best = None
        for _, score, idx in process.extract(game.norm_title, norm, scorer=fuzz.ratio,
                                             limit=5, score_cutoff=85):
            p = pool[pids[idx]]
            raw = RawListing("gog", p["id"], p["title"], p["url"], 0, 0, "USD", release_year=p["year"])
            res = evaluate(game, raw)
            if res.verdict == "match" and (best is None or res.score > best[1]):
                best = (p, res.score)
            elif res.verdict == "review":
                latest = next(iter(prices[p["id"]].values()))[-1]
                reviews.append((res.score, game, RawListing(
                    "gog", p["id"], p["title"], p["url"], latest[3], latest[2], latest[1],
                    region=next(iter(prices[p["id"]])), release_year=p["year"])))
        if not best:
            continue
        p = best[0]
        report["matched"] += 1
        for region, recs in prices[p["id"]].items():
            listing = get_or_create_listing(s, game, "gog", p["id"], region, p["url"])
            have = {_naive(t) for (t,) in s.execute(
                select(PriceSnapshot.captured_at).where(PriceSnapshot.listing_id == listing.id))}
            series = list(recs)
            last = series[-1]
            if last[0] < stamp:  # carry the last known price forward to the dump date
                series.append((stamp, last[1], last[2], last[3]))
            new = []
            for when, cur, base, final in series:
                if _naive(when) in have:
                    continue
                have.add(_naive(when))
                new.append(PriceSnapshot(listing_id=listing.id, price_cents=final, base_price_cents=base,
                                         currency=cur, discount_pct=max(0, round(100 * (1 - final / base))) if base else 0,
                                         captured_at=when))
            s.add_all(new)
            report["snapshots_added"] += len(new)
        s.commit()
    known = {(gid, pid) for gid, pid in s.execute(
        select(MatchCandidate.game_id, MatchCandidate.store_product_id)
        .where(MatchCandidate.store_id == "gog"))}
    reviews = [r for r in reviews if (r[1].id, r[2].product_id) not in known]
    reviews.sort(key=lambda r: -r[0])
    keep = reviews if review_cap is None else reviews[:max(0, review_cap)]
    for score, game, raw in keep:
        queue_review(s, game, raw, score)
    report["review_queued"] = len(keep)
    report["review_dropped"] = len(reviews) - len(keep)
    log_job(s, "gogdb_import", "gog", ",".join(wanted), "ok" if report["matched"] else "no_match",
            f"{report['matched']} games, {report['snapshots_added']} snapshots")
    return report


# ------------------------------------------------------------- download

def archive_url(d: date) -> str:
    return f"{config.GOGDB_BASE_URL}/{d:%Y-%m}/gogdb_{d:%Y-%m-%d}.tar.xz"


def download_latest(cache_dir: str | Path | None = None, lookback_days: int = 4) -> Path:
    """Fetch the newest available daily dump (one file, cached; older ones pruned)."""
    cache = Path(cache_dir or config.GOGDB_CACHE_DIR)
    cache.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).date()
    for back in range(lookback_days + 1):
        d = today - timedelta(days=back)
        dest = cache / f"gogdb_{d:%Y-%m-%d}.tar.xz"
        if dest.exists():
            return dest
        with client() as c, c.stream("GET", archive_url(d), timeout=120) as r:
            if r.status_code == 404:
                continue
            r.raise_for_status()
            tmp = dest.with_suffix(".part")
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
            tmp.rename(dest)
        for old in cache.glob("gogdb_*.tar.xz"):
            if old != dest:
                old.unlink()
        return dest
    raise RuntimeError("no GOGDB dump found in the last %d days" % lookback_days)


def inspect(path: Path, samples: int = 1):
    n, shown, sample = 0, 0, {"product": 0, "prices": 0}
    with tarfile.open(path, "r|*") as tf:
        for m in tf:
            n += 1
            if shown < 25:
                print("member:", m.name, m.size)
                shown += 1
            hit = MEMBER_RE.search(m.name) if m.isfile() else None
            if hit and sample[hit.group(2)] < samples:
                sample[hit.group(2)] += 1
                raw = tf.extractfile(m).read()
                raw = gzip.decompress(raw) if hit.group(3) else raw
                print(f"\n--- sample {m.name} ---\n{raw[:1200].decode('utf-8', 'replace')}\n")
    print("total members:", n)


def main():
    from .db import SessionLocal, init_db
    from .service import seed_stores
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--file")
    ap.add_argument("--inspect")
    args = ap.parse_args()
    if args.inspect:
        return inspect(Path(args.inspect))
    init_db()
    path = Path(args.file) if args.file else download_latest()
    with SessionLocal() as s:
        seed_stores(s)
        print(import_archive(s, path))


if __name__ == "__main__":
    main()
