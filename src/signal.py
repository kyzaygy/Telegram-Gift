"""
Signal resolution: determine the current availability_issued reliably.

Strategy A — poll example_slug.availability_issued directly.
             Only used when M2 confirms the field increments in real time.

Strategy B (default) — probe frontier slugs {stem}-{N} with increasing N.
             The largest N for which the slug exists equals the current frontier.
             Reliable regardless of whether availability_issued is live.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import structlog
from telethon import TelegramClient

from src.tg.gifts import poll_issued

log = structlog.get_logger(__name__)


class FrontierProber:
    """
    Maintains an estimate of the highest issued serial number by probing
    {stem}-{N} slugs. Uses exponential doubling for initial catch-up,
    then linear advance on each tick.
    """

    def __init__(self, client: TelegramClient, stem: str, initial: int) -> None:
        self._client = client
        self._stem = stem
        self._frontier = max(0, initial)
        self._lock = asyncio.Lock()

    async def _exists(self, n: int) -> bool:
        slug = f"{self._stem}-{n}"
        result = await poll_issued(self._client, slug)
        return result is not None

    async def init(self) -> int:
        """Binary-search to find the true current frontier. Call once at startup."""
        async with self._lock:
            if not await self._exists(self._frontier):
                # Walk backward until we find a valid slug
                while self._frontier > 0 and not await self._exists(self._frontier):
                    self._frontier -= 1
                return self._frontier

            # Exponential doubling to find an upper bound
            step = 1
            hi = self._frontier
            while await self._exists(hi + step):
                hi += step
                step *= 2

            # Binary search in [hi, hi+step]
            lo, hi = hi, hi + step
            while lo < hi - 1:
                mid = (lo + hi) // 2
                if await self._exists(mid):
                    lo = mid
                else:
                    hi = mid

            self._frontier = lo
            log.info("frontier_init_done", frontier=self._frontier)
            return self._frontier

    async def advance(self) -> int:
        """Advance frontier by checking if the next slug exists."""
        async with self._lock:
            while await self._exists(self._frontier + 1):
                self._frontier += 1
            return self._frontier


class SignalResolver:
    def __init__(
        self,
        client: TelegramClient,
        example_slug: str,
        slug_stem: str,
        initial_frontier: int,
    ) -> None:
        self._client = client
        self._example_slug = example_slug
        self._stem = slug_stem
        self._prober: Optional[FrontierProber] = (
            FrontierProber(client, slug_stem, initial_frontier) if slug_stem else None
        )
        self._use_strategy_b = True

    async def initialize(self) -> int:
        """Establish initial frontier. Call once before the main loop."""
        if self._prober is not None:
            return await self._prober.init()
        if self._example_slug:
            issued = await poll_issued(self._client, self._example_slug)
            return issued or 0
        return 0

    async def get_current_issued(self) -> Optional[int]:
        if self._use_strategy_b and self._prober is not None:
            return await self._prober.advance()
        if self._example_slug:
            return await poll_issued(self._client, self._example_slug)
        return None

    async def test_strategy_a(self, n_samples: int = 5, interval_sec: float = 2.0) -> bool:
        """
        M2 test: check whether availability_issued changes over multiple samples.
        Returns True if the field is live (strategy A is usable).
        """
        if not self._example_slug:
            return False

        samples: list[int] = []
        for _ in range(n_samples):
            v = await poll_issued(self._client, self._example_slug)
            if v is not None:
                samples.append(v)
            await asyncio.sleep(interval_sec)

        if len(samples) < 2:
            return False

        is_live = max(samples) > min(samples)
        log.info("strategy_a_test", samples=samples, is_live=is_live)
        return is_live

    def prefer_strategy_a(self) -> None:
        self._use_strategy_b = False

    def prefer_strategy_b(self) -> None:
        self._use_strategy_b = True
