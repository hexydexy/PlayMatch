"""Cross-store game matching.

Order of trust:
 1. Steam app ID (exact)
 2. Exact normalized title + release year (within 1 year, when both known)
 3. Fuzzy title >= AUTO_THRESHOLD with compatible year -> auto-match
 4. Fuzzy title >= REVIEW_THRESHOLD -> manual review queue
 Otherwise: reject. Editions/DLC/bundles are never matched to base games.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz import fuzz

from .connectors.base import NON_BASE_MARKERS, RawListing
from .models import Game

AUTO_THRESHOLD = 96
REVIEW_THRESHOLD = 85

_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10"}


def normalize(title: str) -> str:
    title = re.sub(r"[™®©]", " ", title)
    t = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    t = t.lower().replace("&", " and ")
    t = re.sub(r"[™®©]|\(.*?\)", " ", t)
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    words = [_ROMAN.get(w, w) for w in t.split()]
    words = [w for w in words if w not in ("the",)]
    return " ".join(words)


def is_non_base(title: str) -> bool:
    t = title.lower()
    return any(re.search(rf"\b{re.escape(m)}\b", t) for m in NON_BASE_MARKERS)


@dataclass
class MatchResult:
    verdict: str  # match | review | reject
    score: float
    reason: str


def year_compatible(a: int | None, b: int | None) -> bool:
    return a is None or b is None or abs(a - b) <= 1


def evaluate(game: Game, raw: RawListing) -> MatchResult:
    if is_non_base(raw.title) and not is_non_base(game.title):
        return MatchResult("reject", 0, "edition/dlc/bundle")
    if game.steam_app_id and raw.steam_app_id:
        ok = game.steam_app_id == raw.steam_app_id
        return MatchResult("match" if ok else "reject", 100 if ok else 0, "steam_app_id")
    n_raw = normalize(raw.title)
    if n_raw == game.norm_title:
        if year_compatible(game.release_year, raw.release_year):
            return MatchResult("match", 100, "exact_title")
        return MatchResult("review", 90, "exact_title_year_mismatch")
    score = fuzz.ratio(n_raw, game.norm_title)
    if score >= AUTO_THRESHOLD and year_compatible(game.release_year, raw.release_year) \
            and raw.release_year is not None and game.release_year is not None:
        return MatchResult("match", score, "fuzzy_title_year")
    if score >= REVIEW_THRESHOLD:
        return MatchResult("review", score, "fuzzy_title")
    return MatchResult("reject", score, "low_score")
