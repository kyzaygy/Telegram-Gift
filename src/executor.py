"""
Single-shot executor with single-flight guarantee, FLOOD_WAIT handling,
ARM gate, and form-expiry retry.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Optional

import structlog
from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError

from src.state import StateManager
from src.tg.gifts import PaymentFormData, fire_paid, fire_prepaid, prefetch_form

if TYPE_CHECKING:
    from src.shared import AppSharedState

log = structlog.get_logger(__name__)

_locks: dict[int, asyncio.Lock] = {}

# Telethon error strings that indicate a payment form needs re-fetching
_FORM_STALE_HINTS = ("FORM_EXPIRE", "PAYMENT_ID_INVALID", "RECEIPT_RANDOM_ID", "CHARGE_CALL")


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
    shared: Optional["AppSharedState"] = None,
) -> Optional[Any]:
    """
    Execute an upgrade shot.  Returns the Updates object on a live shot,
    None on dry-run, DISARMED, or if the surf was already used.
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

        # Runtime kill-switch from dashboard (ARM/DISARM)
        if shared is not None and not shared.armed:
            log.info("disarmed_abort", msg_id=msg_id)
            return None

        log.info("firing", msg_id=msg_id, is_prepaid=is_prepaid)
        await state.mark_fired(msg_id)

        current_form = form
        for attempt in range(3):
            try:
                if is_prepaid:
                    updates = await fire_prepaid(client, msg_id)
                else:
                    if current_form is None:
                        raise RuntimeError("No payment form available for paid upgrade")
                    updates = await fire_paid(client, current_form)

                log.info("shot_sent", msg_id=msg_id, attempt=attempt)
                return updates

            except FloodWaitError as exc:
                log.warning("flood_wait", seconds=exc.seconds, msg_id=msg_id)
                if attempt < 2:
                    await asyncio.sleep(exc.seconds)
                else:
                    raise

            except RPCError as exc:
                err = str(exc).upper()
                if not is_prepaid and any(k in err for k in _FORM_STALE_HINTS):
                    # Payment form expired — re-fetch and retry once
                    log.warning("form_stale_refetch", attempt=attempt, msg_id=msg_id)
                    if attempt < 2:
                        current_form = await prefetch_form(client, msg_id)
                        continue
                raise

        return None
