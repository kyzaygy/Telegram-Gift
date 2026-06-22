from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TargetStatus:
    target: int
    issue: int = 0
    zone: str = "coarse"          # coarse | mid | tight
    surf_status: str = "watching"  # watching | done | aborted
    result_num: Optional[int] = None


@dataclass
class AppSharedState:
    targets: list[TargetStatus] = field(default_factory=list)
    armed: bool = False
    kill_requested: bool = False
    log_tail: deque = field(default_factory=lambda: deque(maxlen=300))

    def update_target(
        self,
        target_num: int,
        issue: Optional[int] = None,
        zone: Optional[str] = None,
        surf_status: Optional[str] = None,
        result_num: Optional[int] = None,
    ) -> None:
        for t in self.targets:
            if t.target == target_num:
                if issue is not None:
                    t.issue = issue
                if zone is not None:
                    t.zone = zone
                if surf_status is not None:
                    t.surf_status = surf_status
                if result_num is not None:
                    t.result_num = result_num
                break
