from __future__ import annotations

import time

from telethon import TelegramClient

from src.config import Config


async def create_client(cfg: Config) -> TelegramClient:
    client = TelegramClient(
        cfg.env.tg_session,
        cfg.env.tg_api_id,
        cfg.env.tg_api_hash,
    )
    await client.start()
    return client


async def measure_rtt(client: TelegramClient) -> float:
    """Return round-trip time in seconds via a cheap API call."""
    t0 = time.monotonic()
    await client.get_me()
    return time.monotonic() - t0
