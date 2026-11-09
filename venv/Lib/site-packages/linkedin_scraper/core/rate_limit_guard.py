"""Persistent, cross-process guards against LinkedIn rate limiting.

Two independent mechanisms, both backed by small JSON files under
~/.cache/linkedin_scraper/ so they survive across separate script/process
invocations (not just within a single browser session):

- Cooldown: once `detect_rate_limit` sees a rate-limit signal, every
  subsequent call (even from a brand new process) refuses to navigate until
  the suggested wait time has elapsed.
- Write-action pacing: `like`/`repost` (or any other write action) are
  spaced out with a randomized minimum interval, since LinkedIn's abuse
  detection weighs write-action velocity more heavily than reads.

See docs/adr/016-rate-limit-avoidance.md.
"""
import asyncio
import json
import logging
import random
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_CACHE_DIR = Path.home() / ".cache" / "linkedin_scraper"
COOLDOWN_PATH = _CACHE_DIR / "cooldown.json"


class CooldownActiveError(Exception):
    """Raised when a previously recorded rate-limit cooldown hasn't elapsed yet."""

    def __init__(self, message: str, remaining_seconds: int):
        super().__init__(message)
        self.remaining_seconds = remaining_seconds


def record_rate_limit(
    wait_seconds: int, reason: str, path: Path = COOLDOWN_PATH
) -> None:
    """Persist a cooldown window so future runs (even fresh processes) back off."""
    path.parent.mkdir(parents=True, exist_ok=True)
    until = time.time() + wait_seconds
    try:
        path.write_text(
            json.dumps({"until": until, "reason": reason}), encoding="utf-8"
        )
    except OSError as exc:
        logger.debug("rate_limit_guard: could not persist cooldown: %s", exc)
        return
    logger.warning(
        "Cooldown LinkedIn enregistré jusqu'à %s (%s)", time.ctime(until), reason
    )


def check_cooldown(path: Path = COOLDOWN_PATH) -> None:
    """Raise CooldownActiveError if a previously recorded cooldown hasn't elapsed."""
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return

    until = data.get("until", 0)
    remaining = until - time.time()
    if remaining > 0:
        raise CooldownActiveError(
            f"Cooldown LinkedIn actif ({data.get('reason', '?')}) — encore "
            f"{int(remaining)}s avant de pouvoir relancer une action.",
            remaining_seconds=int(remaining),
        )

    # Expired — clean up so future checks stay cheap.
    try:
        path.unlink()
    except OSError:
        pass


async def enforce_write_action_pacing(
    action_key: str,
    min_seconds: float = 20.0,
    jitter_seconds: float = 15.0,
    cache_dir: Path = _CACHE_DIR,
) -> None:
    """Sleep as needed so write actions aren't fired back-to-back.

    A human doesn't like+repost within seconds of each other. This adds
    randomized spacing (min_seconds + random jitter) between calls sharing
    the same action_key, persisted across processes.
    """
    path = cache_dir / f"last_write_{action_key}.json"
    now = time.time()
    last = 0.0
    if path.exists():
        try:
            last = json.loads(path.read_text(encoding="utf-8")).get("ts", 0.0)
        except (OSError, json.JSONDecodeError):
            last = 0.0

    target_gap = min_seconds + random.uniform(0, jitter_seconds)
    elapsed = now - last
    if elapsed < target_gap:
        sleep_for = target_gap - elapsed
        logger.info(
            "Pacing action d'écriture [%s] : attente de %.1fs", action_key, sleep_for
        )
        await asyncio.sleep(sleep_for)

    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps({"ts": time.time()}), encoding="utf-8")
    except OSError as exc:
        logger.debug("rate_limit_guard: could not persist pacing state: %s", exc)


def humanize_delay_ms(base_ms: int, jitter_ratio: float = 0.4) -> int:
    """Return base_ms randomized by +/- jitter_ratio, so fixed waits don't
    form a deterministic timing fingerprint across repeated actions."""
    spread = base_ms * jitter_ratio
    return max(0, int(base_ms + random.uniform(-spread, spread)))
