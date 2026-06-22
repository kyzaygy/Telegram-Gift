"""
Polls can_upgrade / getStarGiftUpgradePreview until upgrades open.
On detection: sets shared.upgrade_open and fires shared.upgrade_detected_event
so sniper loops can react without waiting for the next coarse-poll sleep.
"""

from __future__ import annotations

import asyncio
import time

import structlog
from telethon import TelegramClient

from src.shared import AppSharedState
from src.tg.gifts import SavedGiftInfo, check_upgrade_open

log = structlog.get_logger(__name__)


async def run_release_detector(
    client: TelegramClient,
    gift_id: int,
    surfs: list[SavedGiftInfo],
    shared: AppSharedState,
    poll_interval: float = 4.0,
) -> None:
    """
    Background task.  Polls until upgrade is open, then keeps the flag live.
    """
    surf_msg_id = surfs[0].msg_id if surfs else 0
    log.info("release_detector_started", gift_id=gift_id, poll_sec=poll_interval)

    while not shared.kill_requested:
        try:
            is_open = await check_upgrade_open(client, gift_id, surf_msg_id)
            shared.last_poll_at = time.time()

            if is_open and not shared.upgrade_open:
                shared.upgrade_open = True
                shared.upgrade_detected_event.set()
                log.info("RELEASE_DETECTED", gift_id=gift_id)

            elif not is_open and shared.upgrade_open:
                # Unlikely but handle gracefully (e.g. temporary server error)
                shared.upgrade_open = False
                log.warning("upgrade_closed_unexpectedly")

        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.error("release_detector_error", error=str(exc))

        try:
            await asyncio.sleep(poll_interval)
        except asyncio.CancelledError:
            break

    log.info("release_detector_stopped")
