"""
Entry point.  Starts all asyncio tasks in one event loop:
  • monitor (signal polling + EMA rate)
  • release detector (polls can_upgrade)
  • sniper tasks (one per target)
  • web dashboard (FastAPI/uvicorn on 127.0.0.1:WEB_PORT)
"""

from __future__ import annotations

import asyncio
import sys
from collections import deque
from typing import Optional

import structlog
import structlog.stdlib

from src.config import Config, FirecontrolConfig, TargetConfig, load_config
from src.executor import execute_shot
from src.firecontrol import FireController, SniperState
from src.monitor import Monitor, MonitorSnapshot
from src.release_detector import run_release_detector
from src.result import process_result
from src.shared import AppSharedState, SniperStatus
from src.signal import SignalResolver
from src.state import StateManager, SurfRecord
from src.tg.client import create_client, measure_rtt
from src.tg.gifts import (
    PaymentFormData,
    SavedGiftInfo,
    get_star_balance,
    list_all_upgradeable,
    list_my_surfs,
    poll_gift,
    prefetch_form,
)
from src.web import start_web_server

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Milestones helpers
# ---------------------------------------------------------------------------

async def run_m1(client: object, example_slug: str) -> int:
    log.info("m1_start", slug=example_slug)
    gift = await poll_gift(client, example_slug)  # type: ignore[arg-type]

    if gift is None:
        raise SystemExit(
            f"M1 FAIL — slug '{example_slug}' not found. "
            "Check example_slug in targets.yaml."
        )

    num = getattr(gift, "num", "MISSING")
    issued = getattr(gift, "availability_issued", "MISSING")
    total = getattr(gift, "availability_total", "MISSING")
    slug = getattr(gift, "slug", "MISSING")
    gift_id = getattr(gift, "gift_id", None)

    log.info("m1_pass", num=num, availability_issued=issued,
             availability_total=total, slug=slug, gift_id=gift_id)
    return gift_id or 0


async def run_m3(client: object, gift_id: int, config: Config) -> list[SavedGiftInfo]:
    log.info("m3_start", gift_id=gift_id)

    if gift_id:
        surfs = await list_my_surfs(client, gift_id)  # type: ignore[arg-type]
    else:
        surfs = await list_all_upgradeable(client)  # type: ignore[arg-type]

    log.info("m3_done", count=len(surfs), surfs=[
        {"msg_id": s.msg_id, "upgrade_stars": s.upgrade_stars, "prepaid": s.is_prepaid}
        for s in surfs
    ])
    return surfs


# ---------------------------------------------------------------------------
# Sniper loop
# ---------------------------------------------------------------------------

async def run_sniper(
    client: object,
    target: TargetConfig,
    surfs: list[SavedGiftInfo],
    monitor: Monitor,
    state: StateManager,
    config: Config,
    rtt: float,
    shared: AppSharedState,
    bracket_shift: int = 0,
) -> None:
    if target.ammo_index >= len(surfs):
        log.error("no_ammo", ammo_index=target.ammo_index, available=len(surfs))
        return

    ammo = surfs[target.ammo_index]

    if state.is_surf_used(ammo.msg_id):
        log.warning("surf_already_used_skip", msg_id=ammo.msg_id, target=target.num)
        shared.update_sniper(target.num, surf_status="LOCKED")
        return

    fc = FireController(
        target=target,
        poll=config.poll,
        fc_cfg=config.firecontrol,
        rtt=rtt,
        bracket_shift=bracket_shift,
    )

    form: Optional[PaymentFormData] = None

    log.info(
        "sniper_started",
        target_num=target.num,
        msg_id=ammo.msg_id,
        is_prepaid=ammo.is_prepaid,
        upgrade_stars=ammo.upgrade_stars,
        dry_run=config.runtime.dry_run,
        bracket_shift=bracket_shift,
    )

    while fc.state not in (SniperState.FIRED, SniperState.DONE, SniperState.ABORTED):
        if shared.kill_requested:
            log.info("kill_requested_sniper_exit", target=target.num)
            break

        snap: Optional[MonitorSnapshot] = monitor.snapshot()
        if snap is None:
            await asyncio.sleep(config.poll.armed_sec)
            continue

        decision = fc.evaluate(snap.issued, snap.rate)

        # Update shared state for dashboard
        shared.update_sniper(
            target.num,
            state=fc.state.value,
            issued=snap.issued,
            distance=max(0, target.num - snap.issued),
            rate=snap.rate,
            lead=decision.lead,
            surf_msg_id=ammo.msg_id,
            surf_status=(
                "ABORTED" if fc.is_aborted()
                else ("FIRED" if state.is_surf_used(ammo.msg_id) else "READY")
            ),
        )

        if fc.is_aborted():
            log.warning("sniper_aborted", target=target.num, issued=snap.issued)
            return

        # Wait for upgrade to open before prefetching form
        upgrade_ready = shared.upgrade_open or config.runtime.dry_run

        # Prefetch / refresh payment form in APPROACH/ARMED (paid path only)
        if upgrade_ready and not ammo.is_prepaid and fc.needs_form_refresh():
            try:
                form = await prefetch_form(client, ammo.msg_id)  # type: ignore[arg-type]
                fc.set_form(form)
                log.info("form_refreshed", form_id=form.form_id, target=target.num)
            except Exception as exc:
                log.error("form_prefetch_error", error=str(exc), target=target.num)

        if decision.should_fire and upgrade_ready:
            log.info(
                "firing_trigger",
                target=target.num,
                issued=decision.issued,
                lead=decision.lead,
                fire_threshold=decision.fire_threshold,
                dry_run=config.runtime.dry_run,
            )

            updates = await execute_shot(
                client=client,  # type: ignore[arg-type]
                msg_id=ammo.msg_id,
                form=form,
                is_prepaid=ammo.is_prepaid,
                state=state,
                dry_run=config.runtime.dry_run,
                shared=shared,
            )

            fc.transition_fired()
            shared.update_sniper(target.num, state=SniperState.FIRED.value, surf_status="FIRED")

            if not config.runtime.dry_run and updates is not None:
                result_num = await process_result(
                    client=client,  # type: ignore[arg-type]
                    msg_id=ammo.msg_id,
                    target_num=target.num,
                    updates=updates,
                    state=state,
                )
                fc.transition_done()
                shared.update_sniper(
                    target.num,
                    state=SniperState.DONE.value,
                    result_num=result_num,
                )
                log.info("sniper_done", target=target.num, result_num=result_num)
            else:
                log.info("dry_run_done", target=target.num)
                fc.transition_done()
                shared.update_sniper(target.num, state=SniperState.DONE.value)

            return

        # Sleep — break early if release event fires (wakes coarse-poll snipers)
        sleep_sec = (
            config.poll.armed_sec if fc.state == SniperState.ARMED
            else config.poll.approach_sec if fc.state == SniperState.APPROACH
            else config.poll.coarse_sec
        )
        if fc.state == SniperState.COARSE and not shared.upgrade_open:
            try:
                await asyncio.wait_for(
                    shared.upgrade_detected_event.wait(), timeout=sleep_sec
                )
                log.info("sniper_woken_by_release", target=target.num)
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(sleep_sec)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _main() -> None:
    shared = AppSharedState()
    _setup_logging(shared.log_tail)

    config = load_config("targets.yaml")

    log.info(
        "surfsniper_start",
        dry_run=config.runtime.dry_run,
        targets=[t.num for t in config.targets],
        slug_stem=config.model.slug_stem,
        example_slug=config.model.example_slug,
    )

    if not config.model.slug_stem and not config.model.example_slug:
        log.error("config_missing_slug")
        sys.exit(1)

    # ---- Client ----
    client = await create_client(config)
    shared.session_valid = True

    # ---- M1 ----
    gift_id = 0
    if config.model.example_slug:
        gift_id = await run_m1(client, config.model.example_slug)

    # ---- RTT ----
    rtt = await measure_rtt(client)
    shared.rtt_ms = rtt * 1000
    log.info("rtt", ms=f"{rtt * 1000:.1f}")

    # ---- Star balance ----
    shared.star_balance = await get_star_balance(client)

    # ---- State ----
    state = StateManager()
    await state.load()

    # ---- M3: inventory ----
    surfs = await run_m3(client, gift_id, config)
    if not surfs:
        log.error("no_surfs_found")
        await client.disconnect()
        sys.exit(1)

    surf_records = [SurfRecord(msg_id=s.msg_id, gift_id=s.gift_id) for s in surfs]
    await state.register_surfs(surf_records)

    # ---- Shared sniper status entries ----
    for target in config.targets:
        ammo_idx = target.ammo_index
        msg_id = surfs[ammo_idx].msg_id if ammo_idx < len(surfs) else 0
        shared.snipers.append(SniperStatus(
            target=target.num,
            surf_msg_id=msg_id,
        ))

    # ---- Signal ----
    initial_frontier = config.model.initial_frontier()
    signal = SignalResolver(
        client=client,
        example_slug=config.model.example_slug,
        slug_stem=config.model.slug_stem,
        initial_frontier=initial_frontier,
    )
    current = await signal.initialize()
    log.info("signal_ready", current_issued=current)

    # ---- Monitor ----
    max_target = max(t.num for t in config.targets)
    monitor = Monitor(signal=signal, target_num=max_target, poll=config.poll)
    monitor_task = asyncio.create_task(monitor.run(), name="monitor")

    # ---- Release detector ----
    release_task = asyncio.create_task(
        run_release_detector(
            client=client,
            gift_id=gift_id,
            surfs=surfs,
            shared=shared,
        ),
        name="release_detector",
    )

    # ---- Web dashboard ----
    web_task: Optional[asyncio.Task] = None
    if config.env.web_token:
        web_task = await start_web_server(
            shared=shared,
            token=config.env.web_token,
            host=config.env.web_host,
            port=config.env.web_port,
        )
        log.info("web_started", host=config.env.web_host, port=config.env.web_port)
    else:
        log.info("web_disabled", reason="WEB_TOKEN not set in .env")

    # Allow monitor to get first reading
    await asyncio.sleep(1.0)

    # ---- Bracket shift for second target ----
    def _bracket_shift(i: int) -> int:
        return 1 if (config.firecontrol.bracket and i > 0) else 0

    # ---- Sniper tasks ----
    sniper_tasks = [
        asyncio.create_task(
            run_sniper(
                client=client,
                target=target,
                surfs=surfs,
                monitor=monitor,
                state=state,
                config=config,
                rtt=rtt,
                shared=shared,
                bracket_shift=_bracket_shift(i),
            ),
            name=f"sniper-{target.num}",
        )
        for i, target in enumerate(config.targets)
    ]

    # ---- Monitor kill_requested ----
    async def _kill_watcher() -> None:
        while not shared.kill_requested:
            await asyncio.sleep(0.5)
        log.info("kill_requested_shutdown")
        for t in sniper_tasks:
            t.cancel()

    kill_task = asyncio.create_task(_kill_watcher(), name="kill_watcher")

    try:
        await asyncio.gather(*sniper_tasks, return_exceptions=True)
    finally:
        kill_task.cancel()
        monitor_task.cancel()
        release_task.cancel()
        if web_task:
            web_task.cancel()
        await asyncio.gather(
            monitor_task, release_task, kill_task,
            *(([web_task] if web_task else [])),
            return_exceptions=True,
        )
        await client.disconnect()
        log.info("shutdown")


def main() -> None:
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("\nKill-switch: stopped by user.")


if __name__ == "__main__":
    main()
