"""
Adaptive-polling watcher for one sniper target.

Poll interval by zone:
  coarse  issue < mid_at                  → coarse_sec (60 s)
  mid     mid_at ≤ issue < target-tight_lead → mid_sec  (10 s)
  tight   issue ≥ target - tight_lead     → tight_sec (1.5 s)

Fire condition: issue == target - 1 AND armed == True.
After fire() the watcher exits; if the issue overshoots the target the surf
is aborted (not consumed) and the watcher exits.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog
from pyrogram import Client

from src.config import AppConfig, TargetConfig
from src.fire import fire
from src.issued_probe import current_issue
from src.state import StateManager

if TYPE_CHECKING:
    from src.shared import AppSharedState

log = structlog.get_logger(__name__)


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
        shared.update_target(target.num, issue=issue, zone=zone)

        log.info(
            "tick",
            target=target.num,
            issue=issue,
            distance=distance,
            zone=zone,
            interval=interval,
            armed=shared.armed,
        )

        # Overshoot: target already issued — abort without spending the surf
        if issue >= target.num:
            log.warning("abort_target_passed", target=target.num, issue=issue)
            await state.mark_aborted(msg_id)
            shared.update_target(target.num, issue=issue, surf_status="aborted")
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
                    )
                except Exception as exc:
                    log.error("fire_error", error=str(exc), target=target.num, msg_id=msg_id)
                    await asyncio.sleep(config.intervals.mid_sec)
                    continue

                if num is not None:
                    shared.update_target(
                        target.num, issue=issue, surf_status="done", result_num=num
                    )
                    break
                # fire() returned None: final check may have seen overshoot or disarmed
                # next loop iteration re-probes and handles abort if needed

        await asyncio.sleep(interval)

    log.info("watcher_exit", target=target.num, msg_id=msg_id)
