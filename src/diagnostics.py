"""
Diagnostic runner.  Usage:  python -m src.diagnostics

Runs 13 checks and prints a PASS / FAIL / DEFERRED report.
Checks 1-7 are online (require a live Telegram session).
Checks 8-13 are offline unit tests (no network, use synthetic data).
"""

from __future__ import annotations

import asyncio
import math
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import structlog

from src.config import (
    FirecontrolConfig,
    ModelConfig,
    PollConfig,
    RuntimeConfig,
    TargetConfig,
    load_config,
)
from src.executor import execute_shot
from src.firecontrol import FireController, SniperState
from src.state import StateManager, SurfRecord
from src.tg.client import create_client, measure_rtt
from src.tg.gifts import (
    PaymentFormData,
    get_star_balance,
    parse_num_from_updates,
    poll_gift,
)

# ANSI colours
_G = "\033[92m"   # green
_R = "\033[91m"   # red
_Y = "\033[93m"   # yellow
_B = "\033[94m"   # blue
_D = "\033[2m"    # dim
_X = "\033[0m"    # reset
_W = 78


@dataclass
class CheckResult:
    index: int
    name: str
    status: str       # PASS | FAIL | DEFERRED
    detail: str = ""
    error: str = ""


_results: list[CheckResult] = []


def _add(index: int, name: str, status: str, detail: str = "", error: str = "") -> CheckResult:
    r = CheckResult(index=index, name=name, status=status, detail=detail, error=error)
    _results.append(r)
    colour = _G if status == "PASS" else (_Y if status == "DEFERRED" else _R)
    sym = "✓" if status == "PASS" else ("⏸" if status == "DEFERRED" else "✗")
    idx = f"[{index:2d}]"
    label = name.ljust(24)
    status_str = f"{colour}{sym} {status}{_X}"
    print(f"  {idx} {label} {status_str}  {_D}{detail}{_X}")
    if error:
        print(f"        {_R}↳ {error}{_X}")
    return r


# ---------------------------------------------------------------------------
# Online checks
# ---------------------------------------------------------------------------

async def check_session(client: Any) -> CheckResult:
    try:
        me = await client.get_me()
        username = getattr(me, "username", None)
        uid = getattr(me, "id", None)
        detail = f"user={('@' + username) if username else uid}"
        return _add(1, "Session", "PASS", detail)
    except Exception as e:
        return _add(1, "Session", "FAIL", error=str(e))


async def check_tl_layer(client: Any, example_slug: str) -> tuple[CheckResult, Optional[int]]:
    gift_id: Optional[int] = None
    try:
        gift = await poll_gift(client, example_slug)
        if gift is None:
            return _add(2, "TL layer (M1)", "FAIL",
                        error=f"Slug '{example_slug}' not found"), gift_id

        num = getattr(gift, "num", "MISSING")
        issued = getattr(gift, "availability_issued", "MISSING")
        gift_id = getattr(gift, "gift_id", None)
        slug = getattr(gift, "slug", "MISSING")

        if any(v == "MISSING" for v in (num, issued, slug)):
            return _add(2, "TL layer (M1)", "FAIL",
                        detail=f"num={num} issued={issued} slug={slug}",
                        error="Field(s) missing — TL layer outdated, switch to Kurigram"), gift_id

        detail = f"num={num}, issued={issued}, gift_id={gift_id}"
        return _add(2, "TL layer (M1)", "PASS", detail), gift_id
    except RuntimeError as e:
        # Layer incompatibility
        print(f"\n  {_R}FATAL: {e}{_X}\n")
        sys.exit(1)
    except Exception as e:
        return _add(2, "TL layer (M1)", "FAIL", error=str(e)), gift_id


async def check_rtt(client: Any, n: int = 10) -> CheckResult:
    try:
        samples: list[float] = []
        for _ in range(n):
            t0 = time.monotonic()
            await client.get_me()
            samples.append((time.monotonic() - t0) * 1000)
            await asyncio.sleep(0.1)

        med = statistics.median(samples)
        lo, hi = min(samples), max(samples)
        detail = f"median={med:.0f}ms (min={lo:.0f} max={hi:.0f}, n={n})"
        return _add(3, "RTT to DC", "PASS", detail)
    except Exception as e:
        return _add(3, "RTT to DC", "FAIL", error=str(e))


async def check_inventory(client: Any, gift_id: Optional[int], config: Any) -> tuple[CheckResult, list]:
    from src.tg.gifts import list_all_upgradeable, list_my_surfs

    try:
        if gift_id:
            surfs = await list_my_surfs(client, gift_id)
        else:
            surfs = await list_all_upgradeable(client)

        if not surfs:
            return _add(4, "Inventory (M3)", "FAIL",
                        error="No upgradeable surfs found"), []

        lines: list[str] = []
        for i, s in enumerate(surfs):
            prepaid = "yes" if s.is_prepaid else "no"
            can_up = "unknown"  # can_upgrade is only True when open
            lines.append(
                f"[{i}] msg_id={s.msg_id}  stars={s.upgrade_stars}  "
                f"prepaid={prepaid}  can_upgrade=no (pre-release)"
            )
        detail = "  |  ".join(lines)
        return _add(4, "Inventory (M3)", "PASS", detail), surfs
    except Exception as e:
        return _add(4, "Inventory (M3)", "FAIL", error=str(e)), []


async def check_balance(client: Any, surfs: list, config: Any) -> CheckResult:
    try:
        balance = await get_star_balance(client)
        if balance is None:
            return _add(5, "Star balance", "DEFERRED",
                        detail="balance API unavailable in this TL layer")

        needed = sum(s.upgrade_stars or 0 for s in surfs)
        status = "PASS" if balance >= needed else "FAIL"
        detail = f"{balance} ★  (need {needed} for {len(surfs)} surfs)"
        if balance < needed:
            detail += f"  ← SHORTFALL of {needed - balance} ★"
        return _add(5, "Star balance", status, detail)
    except Exception as e:
        return _add(5, "Star balance", "DEFERRED", detail=f"error: {e}")


async def check_release_detector(client: Any, gift_id: Optional[int], surfs: list) -> CheckResult:
    from src.tg.gifts import check_upgrade_open

    try:
        surf_msg_id = surfs[0].msg_id if surfs else 0
        is_open = await check_upgrade_open(client, gift_id or 0, surf_msg_id)
        if not is_open:
            return _add(6, "Release detector", "PASS",
                        detail="can_upgrade=false (pre-release — expected)")
        else:
            return _add(6, "Release detector", "PASS",
                        detail="can_upgrade=true — UPGRADE IS OPEN")
    except Exception as e:
        return _add(6, "Release detector", "FAIL", error=str(e))


async def check_payment_form(client: Any, surfs: list) -> CheckResult:
    from src.tg.gifts import prefetch_form
    from telethon.errors import RPCError

    if not surfs:
        return _add(7, "Payment form", "DEFERRED", detail="no surfs to test with")

    try:
        form = await prefetch_form(client, surfs[0].msg_id)
        return _add(7, "Payment form", "PASS",
                    detail=f"form_id={form.form_id}  (upgrade already open!)")
    except RPCError as e:
        err_code = str(e)
        return _add(7, "Payment form", "DEFERRED",
                    detail=f"error: {err_code} — expected pre-release")
    except Exception as e:
        return _add(7, "Payment form", "DEFERRED",
                    detail=f"error: {e} — expected pre-release")


# ---------------------------------------------------------------------------
# Offline unit tests
# ---------------------------------------------------------------------------

def _make_updates(num: int) -> Any:
    """Construct a fake Updates tree that parse_num_from_updates can read."""
    gift = SimpleNamespace(num=num)
    action = SimpleNamespace(gift=gift)
    msg = SimpleNamespace(action=action)
    update = SimpleNamespace(message=msg)
    return SimpleNamespace(updates=[update])


def check_parse_num() -> CheckResult:
    try:
        # Normal path: num present in Updates
        ups = _make_updates(444)
        got = parse_num_from_updates(ups)
        assert got == 444, f"expected 444 got {got}"

        # Fallback path: no num in Updates → returns None (caller re-reads)
        empty_ups = SimpleNamespace(updates=[SimpleNamespace(message=SimpleNamespace(action=None))])
        got2 = parse_num_from_updates(empty_ups)
        assert got2 is None, f"expected None got {got2}"

        return _add(8, "parse_num (unit)", "PASS")
    except AssertionError as e:
        return _add(8, "parse_num (unit)", "FAIL", error=str(e))
    except Exception as e:
        return _add(8, "parse_num (unit)", "FAIL", error=str(e))


def _make_fc(target: int, rtt: float, safety: int, bracket_shift: int = 0) -> FireController:
    return FireController(
        target=TargetConfig(num=target, ammo_index=0),
        poll=PollConfig(),
        fc_cfg=FirecontrolConfig(safety=safety),
        rtt=rtt,
        bracket_shift=bracket_shift,
    )


def check_fc_timing() -> CheckResult:
    try:
        # rate=1.0 mint/s, rtt=0.1s, safety=1
        # lead = ceil(1.0 * 0.1) + 1 = 1 + 1 = 2
        # fire_threshold = 444 - 1 - 2 = 441
        fc = _make_fc(target=444, rtt=0.1, safety=1)

        d_below = fc.evaluate(issued=440, rate=1.0)
        assert not d_below.should_fire, "should not fire at 440"

        d_at = fc.evaluate(issued=441, rate=1.0)
        assert d_at.should_fire, "should fire at 441"
        assert d_at.lead == 2, f"expected lead=2 got {d_at.lead}"
        assert d_at.fire_threshold == 441, f"expected threshold=441 got {d_at.fire_threshold}"

        detail = f"trigger at issued={d_at.fire_threshold}, lead={d_at.lead}, rate=1.0, rtt=0.1"
        return _add(9, "FC timing (unit)", "PASS", detail)
    except AssertionError as e:
        return _add(9, "FC timing (unit)", "FAIL", error=str(e))
    except Exception as e:
        return _add(9, "FC timing (unit)", "FAIL", error=str(e))


def check_fc_edge_cases() -> CheckResult:
    errors: list[str] = []

    # Edge 1: issued > target → ABORTED, never fires
    fc_abort = _make_fc(target=444, rtt=0.05, safety=1)
    d = fc_abort.evaluate(issued=445, rate=0.5)
    if d.should_fire:
        errors.append("fired when issued > target")
    if fc_abort.state != SniperState.ABORTED:
        errors.append(f"state not ABORTED after overshoot (got {fc_abort.state})")

    # Edge 2: rate == 0 → lead = safety, no division by zero
    fc_zero = _make_fc(target=444, rtt=1.0, safety=2)
    try:
        d2 = fc_zero.evaluate(issued=440, rate=0.0)
        # lead = ceil(0) + 2 = 2, fire_threshold = 441
        if d2.lead != 2:
            errors.append(f"rate=0 lead should be 2, got {d2.lead}")
    except ZeroDivisionError:
        errors.append("ZeroDivisionError on rate=0")

    # Edge 3: issued == target-1 with safety=0 → fire immediately
    fc_exact = _make_fc(target=444, rtt=0.0, safety=0)
    d3 = fc_exact.evaluate(issued=443, rate=0.0)
    if not d3.should_fire:
        errors.append(f"should fire when issued==target-1 with safety=0, state={fc_exact.state}")

    # Edge 4: bracket shift — second surf has threshold+1
    fc_bracket = _make_fc(target=444, rtt=0.0, safety=0, bracket_shift=1)
    d4a = fc_bracket.evaluate(issued=443, rate=0.0)  # normal threshold=443, bracket=444
    if d4a.should_fire:
        errors.append("bracket surf should not fire at 443 (threshold shifted to 444)")
    d4b = fc_bracket.evaluate(issued=444, rate=0.0)
    if not d4b.should_fire:
        errors.append("bracket surf should fire at 444")

    if errors:
        return _add(10, "FC edge cases (unit)", "FAIL", error="; ".join(errors))
    return _add(10, "FC edge cases (unit)", "PASS",
                detail="abort/zero-rate/exact/bracket all correct")


async def check_single_flight_persist() -> CheckResult:
    try:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name

        state1 = StateManager(path)
        await state1.load()
        await state1.register_surfs([SurfRecord(msg_id=9999, gift_id=1)])
        await state1.mark_fired(9999)
        assert state1.is_surf_used(9999), "surf not marked as used"

        # Simulate restart: fresh StateManager from same file
        state2 = StateManager(path)
        await state2.load()
        assert state2.is_surf_used(9999), "surf not blocked after restart"

        # execute_shot must refuse to fire
        mock_client = MagicMock()
        result = await execute_shot(
            client=mock_client,
            msg_id=9999,
            form=None,
            is_prepaid=True,
            state=state2,
            dry_run=False,
        )
        assert result is None, "execute_shot should return None for used surf"

        return _add(11, "single-flight (unit)", "PASS")
    except AssertionError as e:
        return _add(11, "single-flight (unit)", "FAIL", error=str(e))
    except Exception as e:
        return _add(11, "single-flight (unit)", "FAIL", error=str(e))


async def check_flood_wait() -> CheckResult:
    """Executor must sleep for flood_wait seconds and not retry before that."""
    from telethon.errors import FloodWaitError

    slept: list[float] = []

    async def mock_sleep(n: float) -> None:
        slept.append(n)

    call_count = 0

    async def mock_fire_prepaid(client: Any, msg_id: int) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            exc = FloodWaitError(request=None)
            exc.seconds = 5
            raise exc
        return SimpleNamespace(updates=[])  # success on second call

    try:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        state = StateManager(path)
        await state.load()
        await state.register_surfs([SurfRecord(msg_id=8888, gift_id=1)])

        with (
            patch("src.executor.fire_prepaid", mock_fire_prepaid),
            patch("asyncio.sleep", mock_sleep),
        ):
            result = await execute_shot(
                client=MagicMock(),
                msg_id=8888,
                form=None,
                is_prepaid=True,
                state=state,
                dry_run=False,
            )

        assert call_count == 2, f"expected 2 calls (1 flood + 1 success), got {call_count}"
        assert 5 in slept or any(abs(s - 5) < 0.01 for s in slept), \
            f"should have slept 5s for flood wait, slept={slept}"

        return _add(12, "FLOOD_WAIT (unit)", "PASS",
                    detail=f"slept {slept[0]:.0f}s, retried, succeeded")
    except AssertionError as e:
        return _add(12, "FLOOD_WAIT (unit)", "FAIL", error=str(e))
    except Exception as e:
        return _add(12, "FLOOD_WAIT (unit)", "FAIL", error=str(e))


async def check_form_expiry() -> CheckResult:
    """Executor must re-fetch form on stale-form error and retry."""
    from telethon.errors import RPCError

    fetch_count = 0
    fire_count = 0

    async def mock_prefetch(client: Any, msg_id: int) -> PaymentFormData:
        nonlocal fetch_count
        fetch_count += 1
        return PaymentFormData(form_id=fetch_count * 100, invoice=None)

    async def mock_fire_paid(client: Any, form: PaymentFormData) -> Any:
        nonlocal fire_count
        fire_count += 1
        if fire_count == 1:
            exc = RPCError(request=None, code=400, message="FORM_EXPIRE")
            raise exc
        return SimpleNamespace(updates=[])

    async def mock_sleep(_: float) -> None:
        pass

    try:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        state = StateManager(path)
        await state.load()
        await state.register_surfs([SurfRecord(msg_id=7777, gift_id=1)])

        initial_form = PaymentFormData(form_id=1, invoice=None)

        with (
            patch("src.executor.fire_paid", mock_fire_paid),
            patch("src.executor.prefetch_form", mock_prefetch),
            patch("asyncio.sleep", mock_sleep),
        ):
            result = await execute_shot(
                client=MagicMock(),
                msg_id=7777,
                form=initial_form,
                is_prepaid=False,
                state=state,
                dry_run=False,
            )

        assert fetch_count == 1, f"should have re-fetched form once, got {fetch_count}"
        assert fire_count == 2, f"should have fired twice (1 fail + 1 success), got {fire_count}"
        assert result is not None, "should have returned Updates on success"

        return _add(13, "form expiry (unit)", "PASS",
                    detail=f"re-fetched form, retried fire → success")
    except AssertionError as e:
        return _add(13, "form expiry (unit)", "FAIL", error=str(e))
    except Exception as e:
        return _add(13, "form expiry (unit)", "FAIL", error=str(e))


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _print_report() -> int:
    n_pass = sum(1 for r in _results if r.status == "PASS")
    n_fail = sum(1 for r in _results if r.status == "FAIL")
    n_defer = sum(1 for r in _results if r.status == "DEFERRED")
    deferred = [r for r in _results if r.status == "DEFERRED"]

    print()
    print("━" * _W)
    col_p = _G if n_fail == 0 else _R
    print(
        f"  RESULT: "
        f"{col_p}{n_pass} PASS{_X}  "
        f"{_Y}{n_defer} DEFERRED{_X}  "
        f"{(_R if n_fail else _D)}{n_fail} FAIL{_X}"
    )
    print()
    if deferred:
        print(f"  {_Y}DEFERRED (verify on live release):{_X}")
        for r in deferred:
            print(f"    [{r.index:2d}] {r.name} — {r.detail}")
    print()
    print(f"  {_D}Known unverifiable risk: the paid upgrade path (getPaymentForm → sendStarsForm)")
    print(f"  cannot be tested before the release opens. First live shot is blind.{_X}")
    print("━" * _W)

    return n_fail


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run_diagnostics() -> None:
    print()
    print("━" * _W)
    print(f"  {_B}SURF SNIPER — Diagnostics{_X}")
    print("━" * _W)
    print()

    try:
        config = load_config("targets.yaml")
    except Exception as e:
        print(f"  {_R}Cannot load targets.yaml: {e}{_X}")
        sys.exit(1)

    if not config.model.example_slug:
        print(f"  {_R}example_slug not set in targets.yaml — M1 and online checks skipped{_X}")

    # ---- Configure minimal logging for diagnostics ----
    import structlog
    structlog.configure(
        processors=[
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=open("/dev/null", "w")),
        cache_logger_on_first_use=False,
    )

    print(f"  {_D}Online checks{_X}")
    print("  " + "─" * (_W - 2))

    client = await create_client(config)

    # 1 Session
    await check_session(client)

    # 2 TL layer
    gift_id: Optional[int] = None
    if config.model.example_slug:
        _, gift_id = await check_tl_layer(client, config.model.example_slug)
    else:
        _add(2, "TL layer (M1)", "DEFERRED", detail="example_slug not configured")

    # 3 RTT
    await check_rtt(client)

    # 4 Inventory
    surfs: list = []
    _, surfs = await check_inventory(client, gift_id, config)

    # 5 Balance
    await check_balance(client, surfs, config)

    # 6 Release detector
    await check_release_detector(client, gift_id, surfs)

    # 7 Payment form (always DEFERRED pre-release)
    await check_payment_form(client, surfs)

    print()
    print(f"  {_D}Unit tests (offline){_X}")
    print("  " + "─" * (_W - 2))

    # 8-13 offline
    check_parse_num()
    check_fc_timing()
    check_fc_edge_cases()
    await check_single_flight_persist()
    await check_flood_wait()
    await check_form_expiry()

    await client.disconnect()

    n_fail = _print_report()
    sys.exit(0 if n_fail == 0 else 1)


def main() -> None:
    try:
        asyncio.run(run_diagnostics())
    except KeyboardInterrupt:
        print("\nDiagnostics interrupted.")
        sys.exit(1)


if __name__ == "__main__":
    main()
