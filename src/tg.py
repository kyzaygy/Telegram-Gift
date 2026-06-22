"""
Kurigram (pyrogram) client and gift helpers.
Session file: <TG_SESSION>.session  (created by python -m src.login)
"""

from __future__ import annotations

import os
from typing import Optional

import structlog
from pyrogram import Client
from pyrogram.raw import functions, types

from src.config import AppConfig

log = structlog.get_logger(__name__)


async def create_client(config: AppConfig) -> Client:
    session_file = f"{config.env.tg_session}.session"
    if not os.path.exists(session_file):
        raise RuntimeError(
            f"Session file not found: {session_file}\n"
            "Create it first: python -m src.login"
        )

    app = Client(
        name=config.env.tg_session,
        api_id=config.env.tg_api_id,
        api_hash=config.env.tg_api_hash,
    )
    await app.start()
    me = await app.get_me()
    username = getattr(me, "username", None)
    log.info("client_started", user=f"@{username}" if username else str(me.id))
    return app


async def get_msg_ids(app: Client, gift_id: int) -> list[int]:
    """Return msg_ids of saved surfs with can_upgrade=True and matching gift_id."""
    peer = await app.resolve_peer("me")
    result = await app.invoke(
        functions.payments.GetSavedStarGifts(
            peer=peer,
            offset="",
            limit=100,
        )
    )
    msg_ids: list[int] = []
    for saved in getattr(result, "gifts", []):
        if not getattr(saved, "can_upgrade", False):
            continue
        gift = getattr(saved, "gift", None)
        if gift is None:
            continue
        if getattr(gift, "id", None) != gift_id:
            continue
        msg_id = getattr(saved, "msg_id", None)
        if msg_id is not None:
            msg_ids.append(msg_id)

    log.info("surfs_found", gift_id=gift_id, count=len(msg_ids), msg_ids=msg_ids)
    return msg_ids


async def read_gift_num(app: Client, msg_id: int) -> Optional[int]:
    """Fallback: re-fetch saved gifts and return num of the upgraded surf."""
    peer = await app.resolve_peer("me")
    result = await app.invoke(
        functions.payments.GetSavedStarGifts(
            peer=peer,
            offset="",
            limit=100,
        )
    )
    for saved in getattr(result, "gifts", []):
        if getattr(saved, "msg_id", None) != msg_id:
            continue
        gift = getattr(saved, "gift", None)
        if gift is not None:
            return getattr(gift, "num", None)
    return None
