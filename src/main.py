"""
Entry point.  Build-order: config → client → M1 check → RTT → state →
inventory (M3) → signal init → monitor → sniper tasks.

Each target runs as an independent asyncio task sharing one Monitor.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Optional

import structlog
import structlog.stdlib

from src.config import Config, TargetConfig, load_config
from src.executor import execute_shot
from src.firecontrol import FireController, SniperState
from src.monitor import Monitor, MonitorSnapshot
from src.result import process_result
from src.signal import SignalResolver
from src.state import StateManager, SurfRecord
from src.tg.client import create_client, measure_rtt
from src.tg.gifts import (
    PaymentFormData,
    SavedGiftInfo,
    list_all_upgradeable,
    list_my_surfs,
    poll_gift,
    prefetch_form,
)

log = structlog.get_logger(__name__)


def _setup_logging() -> None:
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


# ---------------------------------------------------------------------------
# Milestones
# ---------------------------------------------------------------------------

async def run_m1(client, example_slug: str) -> int:
    """
    M1: verify TL layer can deserialise starGiftUnique fields.
    Returns gift_id of the base model (needed for M3 filtering).
    """
    log.info("m1_start", slug=example_slug)
    gift = await poll_gift(client, example_slug)

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

    log.info(
        "m1_pass",
        num=num,
        availability_issued=issued,
        availability_total=total,
        slug=slug,
        gift_id=gift_id,
    )
    return gift_id or 0


async def run_m3(client, gift_id: int, config: Config) -> list[SavedGiftInfo]:
    """M3: list owned surfs of this model."""
    log.info("m3_start", gift_id=gift_id)

    if gift_id:
        surfs = await list_my_surfs(client, gift_id)
    else:
        surfs = await list_all_upgradeable(client)

    log.info(
        "m3_done",
        count=len(surfs),
        surfs=[
            {
                "msg_id": s.msg_id,
                "upgrade_stars": s.upgrade_stars,
                "prepaid": s.is_prepaid,
            }
            for s in surfs
        ],
    )
    return surfs


# ---------------------------------------------------------------------------
# Sniper loop (one per target)
# ---------------------------------------------------------------------------

async def run_sniper(
    client,
    target: TargetConfig,
    surfs: list[SavedGiftInfo],
    monitor: Monitor,
    state: StateManager,
    config: Config,
    rtt: float,
) -> None:
    if target.ammo_index >= len(surfs):
        log.error("no_ammo", ammo_index=target.ammo_index, available=len(surfs))
        return

    ammo = surfs[target.ammo_index]

    if state.is_surf_used(ammo.msg_id):
        log.warning("surf_already_used_skip", msg_id=ammo.msg_id, target=target.num)
        return

    fc = FireController(
        target=target,
        poll=config.poll,
        fc_cfg=config.firecontrol,
        rtt=rtt,
    )

    form: Optional[PaymentFormData] = None

    log.info(
        "sniper_started",
        target_num=target.num,
        msg_id=ammo.msg_id,
        is_prepaid=ammo.is_prepaid,
        upgrade_stars=ammo.upgrade_stars,
        dry_run=config.runtime.dry_run,
    )

    while fc.state not in (SniperState.FIRED, SniperState.DONE):
        snap: Optional[MonitorSnapshot] = monitor.snapshot()
        if snap is None:
            await asyncio.sleep(config.poll.armed_sec)
            continue

        decision = fc.evaluate(snap.issued, snap.rate)

        # Prefetch / refresh payment form when approaching target (paid path)
        if not ammo.is_prepaid and fc.needs_form_refresh():
            try:
                form = await prefetch_form(client, ammo.msg_id)
                fc.set_form(form)
                log.info("form_refreshed", form_id=form.form_id, target=target.num)
            except Exception as exc:
                log.error("form_prefetch_error", error=str(exc), target=target.num)

        if decision.should_fire:
            log.info(
                "firing_trigger",
                target=target.num,
                issued=decision.issued,
                lead=decision.lead,
                fire_threshold=decision.fire_threshold,
                dry_run=config.runtime.dry_run,
            )

            updates = await execute_shot(
                client=client,
                msg_id=ammo.msg_id,
                form=form,
                is_prepaid=ammo.is_prepaid,
                state=state,
                dry_run=config.runtime.dry_run,
            )

            fc.transition_fired()

            if not config.runtime.dry_run:
                result_num = await process_result(
                    client=client,
                    msg_id=ammo.msg_id,
                    target_num=target.num,
                    updates=updates,
                    state=state,
                )
                fc.transition_done()
                log.info("sniper_done", target=target.num, result_num=result_num)
            else:
                log.info("dry_run_done", target=target.num)
                fc.transition_done()

            return

        # Sleep between checks (tightest when ARMED)
        if fc.state == SniperState.ARMED:
            await asyncio.sleep(config.poll.armed_sec)
        else:
            await asyncio.sleep(config.poll.approach_sec)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _main() -> None:
    _setup_logging()

    config = load_config("targets.yaml")

    log.info(
        "surfsniper_start",
        dry_run=config.runtime.dry_run,
        targets=[t.num for t in config.targets],
        slug_stem=config.model.slug_stem,
        example_slug=config.model.example_slug,
    )

    if not config.model.slug_stem and not config.model.example_slug:
        log.error("config_missing_slug", msg="Set slug_stem and example_slug in targets.yaml")
        sys.exit(1)

    # ---- Client ----
    client = await create_client(config)

    # ---- M1: TL layer compatibility ----
    gift_id = 0
    if config.model.example_slug:
        gift_id = await run_m1(client, config.model.example_slug)

    # ---- RTT ----
    rtt = await measure_rtt(client)
    log.info("rtt", ms=f"{rtt * 1000:.1f}")

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

    # ---- Monitor (single, tracks toward highest target) ----
    max_target = max(t.num for t in config.targets)
    monitor = Monitor(signal=signal, target_num=max_target, poll=config.poll)
    monitor_task = asyncio.create_task(monitor.run(), name="monitor")

    # Let monitor get at least one reading before snipers start evaluating
    await asyncio.sleep(1.0)

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
            ),
            name=f"sniper-{target.num}",
        )
        for target in config.targets
    ]

    try:
        await asyncio.gather(*sniper_tasks)
    except Exception as exc:
        log.error("sniper_fatal", error=str(exc))
        raise
    finally:
        monitor_task.cancel()
        await asyncio.gather(monitor_task, return_exceptions=True)
        await client.disconnect()
        log.info("shutdown")


def main() -> None:
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("\nKill-switch: stopped by user.")


if __name__ == "__main__":
    main()
