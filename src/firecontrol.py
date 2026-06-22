"""
FSM and trigger logic for a single sniper target.

States:  IDLE → COARSE → APPROACH → ARMED → FIRED → DONE

Fire condition (dynamic lead):
    lead = ceil(rate * rtt) + safety
    fire when: issued >= target_num - 1 - lead

In APPROACH (paid path) we prefetch the payment form and keep it fresh.
In ARMED we run the tightest possible check loop.
"""

from __future__ import annotations

import enum
import math
import time
from dataclasses import dataclass
from typing import Any, Optional

import structlog

from src.config import FirecontrolConfig, PollConfig, TargetConfig

log = structlog.get_logger(__name__)


class SniperState(enum.Enum):
    IDLE = "IDLE"
    COARSE = "COARSE"
    APPROACH = "APPROACH"
    ARMED = "ARMED"
    FIRED = "FIRED"
    DONE = "DONE"
    ABORTED = "ABORTED"    # target num already passed — never fire


@dataclass
class FireDecision:
    should_fire: bool
    issued: int
    rate: float
    lead: int
    fire_threshold: int
    state: SniperState


_FORM_EXPIRY_SEC = 45.0  # refresh payment form before it expires


_TERMINAL = (SniperState.FIRED, SniperState.DONE, SniperState.ABORTED)


class FireController:
    def __init__(
        self,
        target: TargetConfig,
        poll: PollConfig,
        fc_cfg: FirecontrolConfig,
        rtt: float,
        bracket_shift: int = 0,
    ) -> None:
        self._target = target
        self._poll = poll
        self._fc = fc_cfg
        self._rtt = rtt
        self._bracket_shift = bracket_shift
        self._state = SniperState.IDLE
        self._form: Optional[Any] = None
        self._form_fetched_at: float = 0.0

    @property
    def state(self) -> SniperState:
        return self._state

    def _transition(self, new: SniperState) -> None:
        if self._state != new:
            log.info(
                "state_transition",
                target=self._target.num,
                old=self._state.value,
                new=new.value,
            )
            self._state = new

    def evaluate(self, issued: int, rate: float) -> FireDecision:
        """
        Compute lead and fire condition; advance FSM state accordingly.
        Called on every monitor tick.
        """
        target = self._target.num

        # ABORT: target num is already issued — firing would give target+1, wasting the surf.
        if issued >= target and self._state not in _TERMINAL:
            self._transition(SniperState.ABORTED)
            log.warning("target_past_abort", target=target, issued=issued)

        distance = target - issued

        # lead = expected additional mints while our RPC is in flight
        # bracket_shift moves the threshold forward by 1 for the second surf
        lead = math.ceil(rate * self._rtt) + self._fc.safety
        fire_threshold = target - 1 - lead + self._bracket_shift

        # FSM distance-based transitions (terminal states are sticky)
        if self._state not in _TERMINAL:
            if self._state == SniperState.IDLE:
                self._transition(SniperState.COARSE)

            if distance > 50:
                if self._state not in (SniperState.COARSE,):
                    self._transition(SniperState.COARSE)
            elif distance > 10:
                if self._state in (SniperState.IDLE, SniperState.COARSE):
                    self._transition(SniperState.APPROACH)
            else:
                if self._state not in (SniperState.ARMED,):
                    self._transition(SniperState.ARMED)

        should_fire = (
            self._state in (SniperState.APPROACH, SniperState.ARMED)
            and issued >= fire_threshold
        )

        log.debug(
            "evaluate",
            target=target,
            issued=issued,
            rate=f"{rate:.5f}",
            lead=lead,
            fire_threshold=fire_threshold,
            bracket_shift=self._bracket_shift,
            should_fire=should_fire,
            state=self._state.value,
        )

        return FireDecision(
            should_fire=should_fire,
            issued=issued,
            rate=rate,
            lead=lead,
            fire_threshold=fire_threshold,
            state=self._state,
        )

    def is_aborted(self) -> bool:
        return self._state == SniperState.ABORTED

    def needs_form_refresh(self) -> bool:
        """True when we're in approach/armed and the form is missing or stale."""
        if self._state not in (SniperState.APPROACH, SniperState.ARMED):
            return False
        return (time.monotonic() - self._form_fetched_at) > _FORM_EXPIRY_SEC

    def set_form(self, form: Any) -> None:
        self._form = form
        self._form_fetched_at = time.monotonic()

    def get_form(self) -> Optional[Any]:
        return self._form

    def transition_fired(self) -> None:
        self._transition(SniperState.FIRED)

    def transition_done(self) -> None:
        self._transition(SniperState.DONE)
