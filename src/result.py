from __future__ import annotations

from typing import Any, Optional

import structlog
from telethon import TelegramClient

from src.state import StateManager
from src.tg.gifts import parse_num_from_updates, read_surf_num

log = structlog.get_logger(__name__)


async def process_result(
    client: TelegramClient,
    msg_id: int,
    target_num: int,
    updates: Optional[Any],
    state: StateManager,
) -> Optional[int]:
    """
    Extract the assigned num, persist it, and log hit/miss.
    Falls back to re-reading the saved gift if the Updates object
    doesn't carry the num directly.
    """
    num: Optional[int] = None

    if updates is not None:
        try:
            num = parse_num_from_updates(updates)
        except Exception:
            log.warning("parse_num_error_fallback", msg_id=msg_id)
            num = None

    if num is None:
        log.info("num_not_in_updates_fallback_read", msg_id=msg_id)
        num = await read_surf_num(client, msg_id)

    if num is not None:
        await state.mark_done(msg_id, num)
        hit = num == target_num
        log.info(
            "shot_result",
            msg_id=msg_id,
            target=target_num,
            got=num,
            hit=hit,
        )
        if hit:
            log.info("HIT", target=target_num, num=num)
        else:
            log.warning("MISS", target=target_num, got=num, delta=num - target_num)
    else:
        log.error("result_num_unknown", msg_id=msg_id)

    return num
