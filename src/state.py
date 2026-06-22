from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class SurfRecord:
    msg_id: int
    gift_id: int
    status: str = "idle"          # idle | fired | done | aborted
    result_num: Optional[int] = None
    fired_at: Optional[float] = None


@dataclass
class AppState:
    surfs: list[SurfRecord] = field(default_factory=list)


class StateManager:
    def __init__(self, path: str = "state.json") -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._state = AppState()

    async def load(self) -> None:
        try:
            with open(self._path) as f:
                data = json.load(f)
            self._state = AppState(
                surfs=[SurfRecord(**s) for s in data.get("surfs", [])]
            )
        except FileNotFoundError:
            self._state = AppState()

    async def _save(self) -> None:
        with open(self._path, "w") as f:
            json.dump(asdict(self._state), f, indent=2)

    async def register_surfs(self, surfs: list[SurfRecord]) -> None:
        async with self._lock:
            existing = {s.msg_id: s for s in self._state.surfs}
            for surf in surfs:
                if surf.msg_id not in existing:
                    existing[surf.msg_id] = surf
            self._state.surfs = list(existing.values())
            await self._save()

    async def mark_fired(self, msg_id: int) -> None:
        async with self._lock:
            for surf in self._state.surfs:
                if surf.msg_id == msg_id:
                    surf.status = "fired"
                    surf.fired_at = time.time()
                    break
            await self._save()

    async def unmark_fired(self, msg_id: int) -> None:
        """Roll back a fired mark when no RPC was sent (TypeError fallback path)."""
        async with self._lock:
            for surf in self._state.surfs:
                if surf.msg_id == msg_id and surf.status == "fired":
                    surf.status = "idle"
                    surf.fired_at = None
                    break
            await self._save()

    async def mark_done(self, msg_id: int, result_num: Optional[int]) -> None:
        async with self._lock:
            for surf in self._state.surfs:
                if surf.msg_id == msg_id:
                    surf.status = "done"
                    surf.result_num = result_num
                    break
            await self._save()

    async def mark_aborted(self, msg_id: int) -> None:
        """Target number was already issued — surf is saved but cannot reach its target."""
        async with self._lock:
            for surf in self._state.surfs:
                if surf.msg_id == msg_id:
                    surf.status = "aborted"
                    break
            await self._save()

    def is_surf_used(self, msg_id: int) -> bool:
        for surf in self._state.surfs:
            if surf.msg_id == msg_id:
                return surf.status in ("fired", "done", "aborted")
        return False

    def all_surfs(self) -> list[SurfRecord]:
        return list(self._state.surfs)
