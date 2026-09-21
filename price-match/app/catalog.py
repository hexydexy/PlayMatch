"""Sync the list of Steam base games into `games`.

Uses IStoreService/GetAppList, which needs a free Steam Web API key (STEAM_API_KEY).
Games are keyed by Steam app id only: two apps with the same title stay two games.
The response shape below follows Steam's documentation; confirm it against a real
response the first time a key is used (see README, "First run with a key").
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config, service
from .connectors.base import ConnectorError, check_status, client, polite_sleep
from .matcher import normalize
from .models import Game

log = logging.getLogger("playmatch.catalog")

URL = "https://api.steampowered.com/IStoreService/GetAppList/v1/"
PAGE_SIZE = 50000
COMMIT_EVERY = 5000


def parse_page(body: dict) -> tuple[list[tuple[int, str]], bool, int | None]:
    """(apps as (app_id, name), have_more_results, last_appid) from one response page."""
    r = body.get("response") or {}
    apps = [(int(a["appid"]), a["name"].strip()) for a in r.get("apps", [])
            if (a.get("name") or "").strip()]
    return apps, bool(r.get("have_more_results")), r.get("last_appid")


def fetch_all(key: str) -> list[tuple[int, str]]:
    apps: list[tuple[int, str]] = []
    last = 0
    with client() as c:
        while True:
            r = c.get(URL, params={
                "key": key, "include_games": "true", "include_dlc": "false",
                "include_software": "false", "include_videos": "false",
                "include_hardware": "false", "max_results": PAGE_SIZE, "last_appid": last})
            polite_sleep()
            check_status(r, "steam-catalog", (429,))
            page, more, last_id = parse_page(r.json())
            apps.extend(page)
            if not more or not last_id:  # also stops if Steam ever omits the cursor
                break
            last = last_id
    return apps


def sync(s: Session, key: str | None = None, fetch=None) -> dict:
    """Insert new Steam games and refresh renamed ones. Never raises."""
    key = config.STEAM_API_KEY if key is None else key
    if not key:
        log.warning("STEAM_API_KEY is not set; skipping catalog sync")
        return {"skipped": "no STEAM_API_KEY"}
    try:
        apps = (fetch or fetch_all)(key)
    except Exception as e:
        log.error("catalog sync failed: %s", e)
        service.log_job(s, "catalog_sync", "steam", "-", "error", str(e))
        return {"skipped": f"error: {e}"}

    existing = {g.steam_app_id: g for g in s.scalars(select(Game).where(Game.steam_app_id.is_not(None)))}
    added = renamed = pending = 0
    for app_id, name in apps:
        g = existing.get(app_id)
        if g is None:
            g = Game(title=name, norm_title=normalize(name), steam_app_id=app_id)
            s.add(g)
            existing[app_id] = g
            added += 1
        elif g.title != name:
            g.title, g.norm_title = name, normalize(name)
            renamed += 1
        else:
            continue
        pending += 1
        if pending >= COMMIT_EVERY:
            s.commit()
            pending = 0
    s.commit()
    service.log_job(s, "catalog_sync", "steam", "-", "ok", f"{added} added, {renamed} renamed")
    return {"added": added, "renamed": renamed, "total": len(apps)}
