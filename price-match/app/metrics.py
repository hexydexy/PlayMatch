"""Prometheus metrics: request rate/latency (live) and data gauges (at scrape time)."""
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

REQUESTS = Counter("playmatch_http_requests_total", "HTTP requests", ["method", "route", "status"])
LATENCY = Histogram("playmatch_http_request_seconds", "HTTP request latency", ["route"])
GAMES = Gauge("playmatch_games_tracked", "Games tracked")
SNAPSHOTS = Gauge("playmatch_price_snapshots", "Total price snapshots stored")
LISTINGS = Gauge("playmatch_listings", "Listings per store", ["store"])
SUCCESS = Gauge("playmatch_ingest_success_ratio_7d", "Share of ingest runs that succeeded, 7d", ["store"])
PENDING = Gauge("playmatch_pending_match_reviews", "Matches awaiting manual review")


def update_gauges(st: dict):
    GAMES.set(st["games_tracked"])
    SNAPSHOTS.set(st["price_snapshots"])
    PENDING.set(st["pending_reviews"])
    for sid, d in st["stores"].items():
        LISTINGS.labels(sid).set(d["listings"])
        if d.get("success_rate_7d") is not None:
            SUCCESS.labels(sid).set(d["success_rate_7d"])


def render() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
