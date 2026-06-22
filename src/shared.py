"""
In-memory state shared between the bot loop and the web dashboard.
All mutations happen on the asyncio event loop; no extra locking needed.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SniperStatus:
    target: int
    state: str = "IDLE"
    issued: int = 0
    distance: int = 0
    rate: float = 0.0
    lead: int = 0
    surf_msg_id: int = 0
    surf_status: str = "READY"   # READY | ARMED | FIRED | LOCKED | ABORTED
    result_num: Optional[int] = None


@dataclass
class AppSharedState:
    # Per-target live status
    snipers: list[SniperStatus] = field(default_factory=list)

    # Release detector
    upgrade_open: bool = False
    last_poll_at: float = 0.0
    upgrade_detected_event: asyncio.Event = field(default_factory=asyncio.Event)

    # Account
    star_balance: Optional[int] = None
    session_valid: bool = False
    rtt_ms: float = 0.0

    # Dashboard controls
    armed: bool = False           # ARM/DISARM from dashboard
    kill_requested: bool = False

    # Log tail for dashboard
    log_tail: deque[str] = field(default_factory=lambda: deque(maxlen=300))

    def update_sniper(self, target: int, **kwargs: object) -> None:
        for s in self.snipers:
            if s.target == target:
                for k, v in kwargs.items():
                    setattr(s, k, v)
                return

    def get_sniper(self, target: int) -> Optional[SniperStatus]:
        for s in self.snipers:
            if s.target == target:
                return s
        return None
