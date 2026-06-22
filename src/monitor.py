from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional

import structlog

from src.config import PollConfig
from src.signal import SignalResolver

log = structlog.get_logger(__name__)

_EMA_ALPHA_FAR = 0.1
_EMA_ALPHA_NEAR = 0.3


@dataclass
class MonitorSnapshot:
    issued: int
    rate: float   # unique gifts minted per second (EMA)
    timestamp: float


class Monitor:
    """
    Background task that continuously polls the signal and estimates mint rate.
    Adapts polling interval based on distance to the target.
    """

    def __init__(
        self,
        signal: SignalResolver,
        target_num: int,
        poll: PollConfig,
    ) -> None:
        self._signal = signal
        self._target = target_num
        self._poll = poll
        self._snapshot: Optional[MonitorSnapshot] = None
        self._rate: float = 0.0
        self._prev_issued: Optional[int] = None
        self._prev_time: Optional[float] = None
        self._running = False

    def snapshot(self) -> Optional[MonitorSnapshot]:
        return self._snapshot

    def _update_rate(self, issued: int, now: float) -> float:
        if self._prev_issued is not None and self._prev_time is not None:
            dt = now - self._prev_time
            if dt > 0:
                sample = (issued - self._prev_issued) / dt
                if sample >= 0:  # ignore backward jumps (counter reset / clock skew)
                    distance = max(0, self._target - issued)
                    alpha = _EMA_ALPHA_NEAR if distance <= 20 else _EMA_ALPHA_FAR
                    self._rate = alpha * sample + (1 - alpha) * self._rate

        self._prev_issued = issued
        self._prev_time = now
        return self._rate

    def _poll_interval(self, distance: int) -> float:
        if distance > 50:
            return self._poll.coarse_sec
        elif distance > 10:
            return self._poll.approach_sec
        else:
            return self._poll.armed_sec

    async def run(self) -> None:
        self._running = True
        log.info("monitor_started", target=self._target)

        while self._running:
            try:
                issued = await self._signal.get_current_issued()
                if issued is None:
                    await asyncio.sleep(self._poll.coarse_sec)
                    continue

                now = time.monotonic()
                rate = self._update_rate(issued, now)
                distance = max(0, self._target - issued)

                self._snapshot = MonitorSnapshot(
                    issued=issued, rate=rate, timestamp=now
                )

                log.info(
                    "tick",
                    issued=issued,
                    rate=f"{rate:.4f}",
                    distance=distance,
                    target=self._target,
                )

                await asyncio.sleep(self._poll_interval(distance))

            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("monitor_error", error=str(exc))
                await asyncio.sleep(self._poll.coarse_sec)

        self._running = False
        log.info("monitor_stopped", target=self._target)

    def stop(self) -> None:
        self._running = False
