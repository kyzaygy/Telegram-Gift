"""
HTTP-based current-issue probe for Telegram NFT slugs.

Slug existence is determined by fetching https://t.me/nft/{stem}-{N} and
checking the og:title meta tag:
  - Minted:     og:title = "Surge Board #N"  (doesn't start with "Telegram")
  - Not minted: redirect to telegram.org, og:title = "Telegram – a new era of messaging"
  - Network err: OSError — retried by slug_exists(), never treated as "not minted"

current_issue() runs in three phases:
  1. Exponential expansion: double hi until slug doesn't exist → bounds [lo, hi)
  2. Binary search: narrow to approximate boundary (holes ignored for speed)
  3. Linear scan with hole tolerance: advance until hole_tolerance consecutive misses

The linear scan phase makes the result tolerant to individually-missing slug numbers
(non-standard slugs, minting gaps) without overcounting the ceiling.
"""

from __future__ import annotations

import asyncio
import re
import urllib.request

import structlog

log = structlog.get_logger(__name__)

_OG_TITLE_RE = re.compile(r'<meta\s+property="og:title"\s+content="([^"]+)"')


def _slug_exists_sync(stem: str, n: int) -> bool:
    url = f"https://t.me/nft/{stem}-{n}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        html = resp.read().decode("utf-8", "ignore")
    m = _OG_TITLE_RE.search(html)
    if not m:
        return False
    return not m.group(1).startswith("Telegram")


async def slug_exists(stem: str, n: int, retries: int = 3) -> bool:
    """Async wrapper; runs blocking HTTP in a thread executor with exponential retry."""
    loop = asyncio.get_event_loop()
    for attempt in range(retries):
        try:
            return await loop.run_in_executor(None, _slug_exists_sync, stem, n)
        except OSError:
            if attempt == retries - 1:
                log.warning("slug_network_error", stem=stem, n=n)
                raise
            await asyncio.sleep(2.0 ** attempt)
    return False  # unreachable


async def current_issue(stem: str, hole_tolerance: int = 5) -> int:
    """Return the maximum N such that slug stem-N exists."""
    if not await slug_exists(stem, 1):
        return 0

    # Phase 1: exponential expansion
    lo, hi = 1, 2
    while await slug_exists(stem, hi):
        lo = hi
        hi *= 2
    # Invariant: lo exists, hi doesn't

    # Phase 2: binary search (ignores holes — fixed up by linear scan)
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if await slug_exists(stem, mid):
            lo = mid
        else:
            hi = mid

    # Phase 3: linear scan with hole tolerance
    best = lo
    consecutive_miss = 0
    n = lo + 1
    while consecutive_miss < hole_tolerance:
        if await slug_exists(stem, n):
            best = n
            consecutive_miss = 0
        else:
            consecutive_miss += 1
        n += 1

    log.debug("current_issue", stem=stem, issue=best)
    return best
