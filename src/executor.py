"""
Single-shot executor with single-flight guarantee and FLOOD_WAIT handling.

One surf cannot fire twice: the asyncio.Lock per msg_id plus the state
"fired/done" check together prevent double-spend.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import structlog
from telethon import TelegramClient
from telethon.errors import FloodWaitError

from src.state import StateManager
from src.tg.gifts import PaymentFormData, fire_paid, fire_prepaid

log = structlog.get_logger(__name__)

_locks: dict[int, asyncio.Lock] = {}


def _lock_for(msg_id: int) -> asyncio.Lock:
    if msg_id not in _locks:
        _locks[msg_id] = asyncio.Lock()
    return _locks[msg_id]


async def execute_shot(
    client: TelegramClient,
    msg_id: int,
    form: Optional[PaymentFormData],
    is_prepaid: bool,
    state: StateManager,
    dry_run: bool,
) -> Optional[Any]:
    """
    Execute an upgrade shot.  Returns the Updates object on a live shot,
    None on dry-run or if the surf was already used.
    """
    lock = _lock_for(msg_id)

    if lock.locked():
        log.warning("shot_already_in_flight", msg_id=msg_id)
        return None

    async with lock:
        if state.is_surf_used(msg_id):
            log.warning("surf_already_used_abort", msg_id=msg_id)
            return None

        if dry_run:
            log.info(
                "DRY_RUN_would_fire",
                msg_id=msg_id,
                is_prepaid=is_prepaid,
                form_id=form.form_id if form else None,
            )
            return None

        log.info("firing", msg_id=msg_id, is_prepaid=is_prepaid)
        await state.mark_fired(msg_id)

        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                if is_prepaid:
                    updates = await fire_prepaid(client, msg_id)
                else:
                    if form is None:
                        raise RuntimeError("No payment form available for paid upgrade")
                    updates = await fire_paid(client, form)

                log.info("shot_sent", msg_id=msg_id, attempt=attempt)
                return updates

            except FloodWaitError as exc:
                log.warning("flood_wait", seconds=exc.seconds, msg_id=msg_id)
                if attempt < 2:
                    await asyncio.sleep(exc.seconds)
                else:
                    raise

            except Exception as exc:
                log.error("shot_error", error=str(exc), msg_id=msg_id, attempt=attempt)
                last_exc = exc
                raise

        if last_exc:
            raise last_exc
        return None
