"""Disk cache mapping a feed card's URN to its resolved LinkedIn permalink.

Resolving a permalink for a card that only exposes a `compkey` in the DOM
requires opening its overflow menu and reading the clipboard (see
FeedScraper._fill_missing_permalinks_from_ui). That's real UI interaction
against a live LinkedIn session, so re-resolving the same post's permalink on
every scrape is wasted, avoidable interaction volume — a real driver of
rate-limit/bot detection. Caching by URN means repeat scrapes of the same
feed only pay that cost once.

See docs/adr/016-rate-limit-avoidance.md.
"""
import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_CACHE_PATH = Path.home() / ".cache" / "linkedin_scraper" / "permalink_cache.json"


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def get_cached_permalink(urn: str, path: Path = DEFAULT_CACHE_PATH) -> Optional[str]:
    if not urn:
        return None
    return _load(path).get(urn)


def save_cached_permalink(urn: str, permalink: str, path: Path = DEFAULT_CACHE_PATH) -> None:
    if not urn or not permalink:
        return
    cache = _load(path)
    if cache.get(urn) == permalink:
        return
    cache[urn] = permalink
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(cache), encoding="utf-8")
    except OSError as exc:
        logger.debug("permalink_cache: write failed: %s", exc)
