"""
Final-check → payment-form → payment → result.

Only fires when current_issue() == target_num - 1 exactly.
If the issue has already reached or passed the target, returns None immediately
(caller marks surf as aborted — the surf is NOT consumed).
"""

from __future__ import annotations

from typing import Optional

import structlog
from pyrogram import Client
from pyrogram.raw import functions, types

from src.issued_probe import current_issue
from src.state import StateManager
from src.tg import read_gift_num

log = structlog.get_logger(__name__)


def _parse_num(result: object) -> Optional[int]:
    """
    Extract the assigned num from payments.PaymentResult.
    PaymentResult.updates is an Updates object (not a list); its .updates list
    contains service messages with messageActionStarGiftUnique.gift.num.
    """
    try:
        outer = getattr(result, "updates", None)
        if outer is None:
            return None
        updates_list = getattr(outer, "updates", None)
        if not isinstance(updates_list, list):
            return None
        for upd in updates_list:
            try:
                msg = getattr(upd, "message", None)
                if msg is None:
                    continue
                action = getattr(msg, "action", None)
                if action is None:
                    continue
                gift = getattr(action, "gift", None)
                if gift is None:
                    continue
                num = getattr(gift, "num", None)
                if num is not None:
                    return int(num)
            except Exception:
                continue
        return None
    except Exception:
        return None


async def fire(
    app: Client,
    msg_id: int,
    target_num: int,
    stem: str,
    hole_tolerance: int,
    state: StateManager,
    armed: bool,
) -> Optional[int]:
    """
    Perform a fresh HTTP probe, fetch the payment form, and send the upgrade.
    Returns the assigned collectible num, or None if aborted / not ready / disarmed.
    """
    # Fresh final check before committing
    issue = await current_issue(stem, hole_tolerance)
    log.info("final_check", target=target_num, issue=issue, msg_id=msg_id)

    if issue >= target_num:
        log.warning("final_check_overshoot", target=target_num, issue=issue)
        return None  # caller detects overshoot on next watcher iteration

    if issue != target_num - 1:
        log.info("final_check_not_ready", target=target_num, issue=issue)
        return None

    if not armed:
        log.info("final_check_disarmed", target=target_num)
        return None

    # Build invoice for this surf
    stargift = types.InputSavedStarGiftUser(msg_id=msg_id)
    invoice = types.InputInvoiceStarGiftUpgrade(
        stargift=stargift,
        keep_original_details=False,
    )

    # Fetch payment form (must be fresh — forms expire in ~45 s)
    form_result = await app.invoke(
        functions.payments.GetPaymentForm(invoice=invoice)
    )
    form_id = form_result.form_id
    log.info("payment_form_fetched", form_id=form_id, msg_id=msg_id)

    # Mark fired before the RPC so a process crash doesn't double-fire on restart
    await state.mark_fired(msg_id)
    try:
        try:
            result = await app.invoke(
                functions.payments.SendStarsForm(form_id=form_id, invoice=invoice)
            )
        except TypeError:
            # Some TL layers omit the invoice param; fall back to form_id only
            result = await app.invoke(
                functions.payments.SendStarsForm(form_id=form_id)
            )
    except Exception:
        # No payment confirmed — roll back so the surf isn't stranded as "fired"
        await state.unmark_fired(msg_id)
        raise

    num = _parse_num(result)
    if num is None:
        log.warning("num_not_in_result_fallback", msg_id=msg_id)
        num = await read_gift_num(app, msg_id)

    await state.mark_done(msg_id, num)

    if num == target_num:
        log.info("HIT", target=target_num, num=num, msg_id=msg_id)
    else:
        log.warning("MISS", target=target_num, got=num, delta=(num or 0) - target_num, msg_id=msg_id)

    return num
