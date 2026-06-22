"""
Entry point. Starts two watcher tasks (one per target) and an optional web dashboard.
"""

from __future__ import annotations

import asyncio
import sys
from collections import deque

import structlog
import structlog.stdlib

from src.config import load_config
from src.shared import AppSharedState, TargetStatus
from src.state import StateManager, SurfRecord
from src.tg import create_client, get_msg_ids
from src.watcher import watch_target
from src.web import start_web_server

log = structlog.get_logger(__name__)


def _setup_logging(log_tail: deque) -> None:
    def _tail_proc(logger: object, method: str, event_dict: dict) -> dict:
        try:
            ts = str(event_dict.get("timestamp", ""))[:23]
            level = str(event_dict.get("level", "")).upper()[:4]
            event = str(event_dict.get("event", ""))
            extra = {
                k: v for k, v in event_dict.items()
                if k not in ("timestamp", "level", "event", "_record", "logger")
            }
            parts = [f"[{ts}]", level, event]
            if extra:
                parts.append(" ".join(f"{k}={v}" for k, v in list(extra.items())[:6]))
            log_tail.append(" ".join(parts))
        except Exception:
            pass
        return event_dict

    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _tail_proc,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


async def _main() -> None:
    log_tail: deque = deque(maxlen=300)
    _setup_logging(log_tail)

    config = load_config("targets.yaml")

    log.info(
        "surfsniper_start",
        armed=config.runtime.armed,
        targets=[t.num for t in config.targets],
        slug_stem=config.model.slug_stem,
        gift_id=config.model.gift_id,
    )

    if not config.model.slug_stem:
        log.error("config_missing_slug_stem")
        sys.exit(1)

    shared = AppSharedState(
        targets=[TargetStatus(target=t.num) for t in config.targets],
        armed=config.runtime.armed,
        log_tail=log_tail,
    )

    state = StateManager()
    await state.load()

    app = await create_client(config)

    msg_ids = await get_msg_ids(app, config.model.gift_id)
    if len(msg_ids) < len(config.targets):
        log.error("not_enough_surfs", found=len(msg_ids), needed=len(config.targets))
        await app.stop()
        sys.exit(1)

    await state.register_surfs([
        SurfRecord(msg_id=mid, gift_id=config.model.gift_id)
        for mid in msg_ids
    ])

    tasks: list[asyncio.Task] = []

    if config.env.web_token:
        web_task = await start_web_server(
            shared=shared,
            token=config.env.web_token,
            host=config.env.web_host,
            port=config.env.web_port,
        )
        tasks.append(web_task)
        log.info("web_started", host=config.env.web_host, port=config.env.web_port)

    for target in config.targets:
        if target.ammo_index >= len(msg_ids):
            log.error("ammo_index_out_of_range", ammo_index=target.ammo_index, target=target.num)
            continue
        msg_id = msg_ids[target.ammo_index]
        task = asyncio.create_task(
            watch_target(app, config, target, msg_id, state, shared),
            name=f"watcher-{target.num}",
        )
        tasks.append(task)

    async def _kill_watcher() -> None:
        while not shared.kill_requested:
            await asyncio.sleep(0.5)
        log.info("kill_requested_shutdown")
        for t in tasks:
            if not t.done():
                t.cancel()

    kill_task = asyncio.create_task(_kill_watcher(), name="kill_watcher")

    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        kill_task.cancel()
        await asyncio.gather(kill_task, return_exceptions=True)
        await app.stop()
        log.info("shutdown")


def main() -> None:
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("\nKill-switch: stopped by user.")


if __name__ == "__main__":
    main()
