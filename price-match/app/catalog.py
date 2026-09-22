"""Sync the list of Steam base games into `games`.

Uses IStoreService/GetAppList, which needs a free Steam Web API key (STEAM_API_KEY).
Games are keyed by Steam app id only: two apps with the same title stay two games.
The response shape below follows Steam's documentation; confirm it against a real
response the first time a key is used (see README, "First run with a key").
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config, service
from .connectors.base import ConnectorError, check_status, client, polite_sleep
from .matcher import normalize
from .models import Game

log = logging.getLogger("playmatch.catalog")


class _RedactKeyFilter(logging.Filter):
    """Redacts API key query parameter from log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact key= query parameter and return True to allow the record."""
        # Redact record.msg if it's a string
        if isinstance(record.msg, str):
            record.msg = re.sub(r'key=[^&\s"]+', 'key=REDACTED', record.msg)

        # Redact record.args - convert to string only when checking for key=, preserve types
        if record.args:
            if isinstance(record.args, tuple):
                # Check if any arg contains 'key=' and redact it
                new_args = []
                for arg in record.args:
                    arg_str = str(arg)
                    if 'key=' in arg_str:
                        # Only convert to string if it contains the secret
                        new_args.append(re.sub(r'key=[^&\s"]+', 'key=REDACTED', arg_str))
                    else:
                        new_args.append(arg)
                record.args = tuple(new_args)
            elif isinstance(record.args, dict):
                record.args = {
                    k: re.sub(r'key=[^&\s"]+', 'key=REDACTED', str(v))
                    if 'key=' in str(v) else v
                    for k, v in record.args.items()
                }

        return True


# Attach the filter to httpx logger at module import time
_httpx_logger = logging.getLogger("httpx")
if not any(isinstance(f, _RedactKeyFilter) for f in _httpx_logger.filters):
    _httpx_logger.addFilter(_RedactKeyFilter())

URL = "https://api.steampowered.com/IStoreService/GetAppList/v1/"
PAGE_SIZE = 50000
MAX_PAGES = 100  # safety stop for the cursor loop; 100 pages of 50,000 is far more than Steam lists
COMMIT_EVERY = 5000
MAX_APP_ID = 2**31 - 1  # steam_app_id is an Integer column
MAX_NAME_LEN = 300  # games.title is String(300)


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
        for _ in range(MAX_PAGES):
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
            if last_id <= last:  # a cursor that does not advance would repeat the same page forever
                log.warning("catalog fetch stopped: cursor %s did not advance past %s", last_id, last)
                break
            last = last_id
        else:
            log.warning("catalog fetch stopped after %d pages (MAX_PAGES)", MAX_PAGES)
    return apps


def sync(s: Session, key: str | None = None, fetch=None) -> dict:
    """Insert new Steam games and refresh renamed ones. Never raises: failures are logged and returned as `skipped`."""
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

    try:
        existing = {g.steam_app_id: g for g in s.scalars(select(Game).where(Game.steam_app_id.is_not(None)))}
        added = renamed = pending = invalid = 0
        for app_id, name in apps:
            if not 1 <= app_id <= MAX_APP_ID:  # would not fit the Integer column
                invalid += 1
                continue
            name = name.replace("\x00", "")[:MAX_NAME_LEN]  # Postgres rejects NUL; the column is String(300)
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
    except Exception as e:
        log.exception("catalog sync failed while saving: %s", e)
        s.rollback()
        service.log_job(s, "catalog_sync", "steam", "-", "error", str(e))
        return {"skipped": f"error: {e}"}
    report = {"added": added, "renamed": renamed, "total": len(apps)}
    if invalid:
        log.warning("catalog sync skipped %d apps with an out-of-range id", invalid)
        report["invalid"] = invalid
    return report
