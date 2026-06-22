"""
Adaptive-polling watcher for one sniper target.

Poll interval by zone:
  coarse  issue < mid_at                       → coarse_sec (60 s)
  mid     mid_at <= issue < target-tight_lead  → mid_sec  (10 s)
  tight   issue >= target - tight_lead         → tight_sec (0.3 s)

In tight zone the payment form is prefetched and refreshed every 30 s so that
the actual fire() call only needs to send SendStarsForm.

Fire condition: issue == target - 1 AND armed == True.
Overshoot (issue >= target) → surf marked aborted, watcher exits.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Optional

import structlog
from pyrogram import Client

from src.config import AppConfig, TargetConfig
from src.fire import fetch_payment_form, fire
from src.issued_probe import current_issue
from src.state import StateManager

if TYPE_CHECKING:
    from src.shared import AppSharedState

log = structlog.get_logger(__name__)

_FORM_TTL = 30.0  # refresh prefetched form after this many seconds


def _zone(issue: int, target: int, config: AppConfig) -> str:
    if issue >= target - config.zones.tight_lead:
        return "tight"
    if issue >= config.zones.mid_at:
        return "mid"
    return "coarse"


def _interval(zone: str, config: AppConfig) -> float:
    return {"tight": config.intervals.tight_sec, "mid": config.intervals.mid_sec}.get(
        zone, config.intervals.coarse_sec
    )


async def watch_target(
    app: Client,
    config: AppConfig,
    target: TargetConfig,
    msg_id: int,
    state: StateManager,
    shared: "AppSharedState",
) -> None:
    log.info("watcher_start", target=target.num, msg_id=msg_id)

    prefetched_form_id: Optional[int] = None
    prefetched_invoice: Optional[object] = None
    form_fetched_at: float = 0.0

    while not shared.kill_requested:
        if state.is_surf_used(msg_id):
            log.info("watcher_surf_done_on_start", msg_id=msg_id, target=target.num)
            break

        try:
            issue = await current_issue(config.model.slug_stem, config.probe.hole_tolerance)
        except Exception as exc:
            log.warning("probe_error", error=str(exc), target=target.num)
            await asyncio.sleep(config.intervals.mid_sec)
            continue

        distance = target.num - issue
        zone = _zone(issue, target.num, config)
        interval = _interval(zone, config)
        shared.update_target(target.num, issue=issue, zone=zone, interval=interval)

        log.info(
            "tick",
            target=target.num,
            issue=issue,
            distance=distance,
            zone=zone,
            interval=interval,
            armed=shared.armed,
        )

        # Prefetch payment form in tight zone; refresh when stale
        if zone == "tight":
            now = time.monotonic()
            if prefetched_form_id is None or now - form_fetched_at > _FORM_TTL:
                try:
                    prefetched_form_id, prefetched_invoice = await fetch_payment_form(app, msg_id)
                    form_fetched_at = now
                    log.debug("form_prefetched", form_id=prefetched_form_id, target=target.num)
                except Exception as exc:
                    log.warning("form_prefetch_error", error=str(exc), target=target.num)
                    prefetched_form_id = None
        else:
            # Outside tight zone: clear any stale prefetch
            prefetched_form_id = None
            prefetched_invoice = None

        # Overshoot: target already issued — abort without spending the surf
        if issue >= target.num:
            log.warning("abort_target_passed", target=target.num, issue=issue)
            await state.mark_aborted(msg_id)
            shared.update_target(target.num, surf_status="aborted")
            break

        # Trigger condition
        if issue == target.num - 1:
            log.info("trigger_condition_met", target=target.num, issue=issue, armed=shared.armed)
            if not shared.armed:
                log.warning("trigger_but_disarmed", target=target.num)
            else:
                try:
                    num = await fire(
                        app=app,
                        msg_id=msg_id,
                        target_num=target.num,
                        stem=config.model.slug_stem,
                        hole_tolerance=config.probe.hole_tolerance,
                        state=state,
                        armed=shared.armed,
                        prefetched_form_id=prefetched_form_id,
                        prefetched_invoice=prefetched_invoice,
                    )
                except Exception as exc:
                    log.error("fire_error", error=str(exc), target=target.num, msg_id=msg_id)
                    await asyncio.sleep(config.intervals.mid_sec)
                    continue

                if num is not None:
                    shared.update_target(
                        target.num, surf_status="done", result_num=num
                    )
                    break

        await asyncio.sleep(interval)

    log.info("watcher_exit", target=target.num, msg_id=msg_id)
