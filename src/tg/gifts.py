"""
Thin wrappers around Telegram gift-related MTProto calls.

Constructor hashes used here (verify against installed Telethon layer):
  GetUniqueStarGiftRequest     #a1974d72
  payments.UniqueStarGift      #416c56e8
  starGiftUnique               #1befe865
  inputSavedStarGiftUser       #69279795
  inputInvoiceStarGiftUpgrade  #4d818d5d
  UpgradeStarGiftRequest       #aed6e4f5
  messageActionStarGiftUnique  #34f762f3

If Telethon's TL layer doesn't recognise these, switch to Kurigram (see README).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import structlog
from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError
from telethon.tl import functions, types

log = structlog.get_logger(__name__)


@dataclass
class SavedGiftInfo:
    msg_id: int
    gift_id: int
    upgrade_stars: Optional[int]
    is_prepaid: bool
    prepaid_hash: Optional[str]


@dataclass
class PaymentFormData:
    form_id: int
    invoice: Any  # InputInvoiceStarGiftUpgrade instance


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

async def poll_gift(client: TelegramClient, slug: str) -> Optional[Any]:
    """
    Call getUniqueStarGift(slug) and return the starGiftUnique object,
    or None if the slug does not exist yet.
    """
    try:
        result = await client(functions.payments.GetUniqueStarGiftRequest(slug=slug))
        return result.gift
    except AttributeError:
        raise RuntimeError(
            "GetUniqueStarGiftRequest not found in this Telethon TL layer. "
            "Switch to Kurigram — see README M1 section."
        )
    except RPCError as e:
        msg = str(e).upper()
        if any(k in msg for k in ("NOT_FOUND", "SLUG_INVALID", "GIFT_SLUG", "INVALID")):
            return None
        raise


async def poll_issued(client: TelegramClient, slug: str) -> Optional[int]:
    """Return availability_issued for an existing slug, or None if slug not found."""
    gift = await poll_gift(client, slug)
    if gift is None:
        return None
    return getattr(gift, "availability_issued", None)


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

async def list_my_surfs(client: TelegramClient, gift_id: int) -> list[SavedGiftInfo]:
    """
    Fetch all saved star gifts and return those matching gift_id with can_upgrade set.
    Handles parameter name variation between Telethon versions via fallback.
    """
    me = await client.get_me()
    peer = await client.get_input_entity(me)

    surfs: list[SavedGiftInfo] = []
    offset: str = ""
    limit = 100

    while True:
        result = await _fetch_saved_gifts(client, peer, offset, limit)

        for saved in result.gifts:
            base_gift = getattr(saved, "gift", None)
            if base_gift is None:
                continue
            if getattr(base_gift, "id", None) != gift_id:
                continue

            msg_id: Optional[int] = getattr(saved, "msg_id", None)
            if msg_id is None:
                continue

            upgrade_stars: Optional[int] = getattr(saved, "upgrade_stars", None)
            prepaid_hash: Optional[str] = getattr(saved, "prepaid_upgrade_hash", None)

            surfs.append(SavedGiftInfo(
                msg_id=msg_id,
                gift_id=gift_id,
                upgrade_stars=upgrade_stars,
                is_prepaid=prepaid_hash is not None,
                prepaid_hash=prepaid_hash,
            ))

        next_offset: Optional[str] = getattr(result, "next_offset", None)
        if not next_offset or not result.gifts:
            break
        offset = next_offset

    return surfs


async def list_all_upgradeable(client: TelegramClient) -> list[SavedGiftInfo]:
    """
    List all saved star gifts where can_upgrade is set.
    Used when gift_id is not yet known.
    """
    me = await client.get_me()
    peer = await client.get_input_entity(me)

    surfs: list[SavedGiftInfo] = []
    offset: str = ""
    limit = 100

    while True:
        result = await _fetch_saved_gifts(client, peer, offset, limit)

        for saved in result.gifts:
            can_upgrade = getattr(saved, "can_upgrade", False)
            if not can_upgrade:
                continue

            base_gift = getattr(saved, "gift", None)
            msg_id: Optional[int] = getattr(saved, "msg_id", None)
            if base_gift is None or msg_id is None:
                continue

            gift_id: int = getattr(base_gift, "id", 0)
            upgrade_stars: Optional[int] = getattr(saved, "upgrade_stars", None)
            prepaid_hash: Optional[str] = getattr(saved, "prepaid_upgrade_hash", None)

            surfs.append(SavedGiftInfo(
                msg_id=msg_id,
                gift_id=gift_id,
                upgrade_stars=upgrade_stars,
                is_prepaid=prepaid_hash is not None,
                prepaid_hash=prepaid_hash,
            ))

        next_offset = getattr(result, "next_offset", None)
        if not next_offset or not result.gifts:
            break
        offset = next_offset

    return surfs


async def _fetch_saved_gifts(client: TelegramClient, peer: Any, offset: str, limit: int) -> Any:
    """Call GetSavedStarGiftsRequest, trying different parameter signatures."""
    # Try standard signature first
    try:
        return await client(functions.payments.GetSavedStarGiftsRequest(
            peer=peer,
            offset=offset,
            limit=limit,
        ))
    except TypeError:
        pass

    # Some versions use user_id instead of peer
    try:
        return await client(functions.payments.GetSavedStarGiftsRequest(
            user_id=peer,
            offset=offset,
            limit=limit,
        ))
    except TypeError:
        pass

    # Minimal call
    return await client(functions.payments.GetSavedStarGiftsRequest(
        peer=peer,
        offset=offset,
        limit=limit,
    ))


# ---------------------------------------------------------------------------
# Payment form + firing
# ---------------------------------------------------------------------------

def _build_stargift_input(msg_id: int) -> Any:
    return types.InputSavedStarGiftUser(msg_id=msg_id)


def _build_invoice(msg_id: int) -> Any:
    return types.InputInvoiceStarGiftUpgrade(
        stargift=_build_stargift_input(msg_id),
        keep_original_details=False,
    )


async def prefetch_form(client: TelegramClient, msg_id: int) -> PaymentFormData:
    invoice = _build_invoice(msg_id)
    try:
        form = await client(functions.payments.GetPaymentFormRequest(invoice=invoice))
    except TypeError:
        # Some versions require theme_params
        form = await client(functions.payments.GetPaymentFormRequest(
            invoice=invoice,
            theme_params=None,
        ))
    return PaymentFormData(form_id=form.form_id, invoice=invoice)


async def fire_prepaid(client: TelegramClient, msg_id: int) -> Any:
    stargift = _build_stargift_input(msg_id)
    return await client(functions.payments.UpgradeStarGiftRequest(
        stargift=stargift,
        keep_original_details=False,
    ))


async def fire_paid(client: TelegramClient, form: PaymentFormData) -> Any:
    try:
        return await client(functions.payments.SendStarsFormRequest(
            form_id=form.form_id,
            invoice=form.invoice,
        ))
    except TypeError:
        # Some TL layers omit the invoice param; fall back to form_id only
        return await client(functions.payments.SendStarsFormRequest(
            form_id=form.form_id,
        ))


# ---------------------------------------------------------------------------
# Result parsing
# ---------------------------------------------------------------------------

def parse_num_from_updates(updates: Any) -> Optional[int]:
    """
    Walk the Updates object looking for messageActionStarGiftUnique.gift.num.
    Handles both bare Updates (upgradeStarGift path) and PaymentResult wrapper
    (sendStarsForm path where .updates is an Updates object, not a list).
    """
    if updates is None:
        return None

    try:
        raw = getattr(updates, "updates", None)
        if raw is not None and not isinstance(raw, list):
            # PaymentResult path: .updates is an Updates object — unwrap one level
            raw = getattr(raw, "updates", []) or []
        elif raw is None:
            raw = []

        for update in raw:
            try:
                msg = getattr(update, "message", None)
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


# ---------------------------------------------------------------------------
# Account helpers
# ---------------------------------------------------------------------------

async def get_star_balance(client: TelegramClient) -> Optional[int]:
    """
    Return the account's Telegram Star balance.
    Uses payments.getStarsStatus — may not exist in older layers; returns None on error.
    """
    try:
        me = await client.get_me()
        peer = await client.get_input_entity(me)
        result = await client(functions.payments.GetStarsStatusRequest(peer=peer))
        balance = getattr(result, "balance", None)
        if balance is None:
            return None
        if hasattr(balance, "amount"):
            return int(balance.amount)
        return int(balance)
    except Exception:
        return None


async def check_upgrade_open(
    client: TelegramClient,
    gift_id: int,
    surf_msg_id: int,
) -> bool:
    """
    Return True if the upgrade for this gift model is currently available.

    Tries two methods in order:
    1. getStarGiftUpgradePreview(gift_id) — if available in TL layer.
    2. Fallback: re-read the saved gift and check can_upgrade flag.
    """
    # Method 1: preview call
    try:
        await client(functions.payments.GetStarGiftUpgradePreviewRequest(gift_id=gift_id))
        return True
    except RPCError as e:
        err = str(e).upper()
        # Known pre-release errors
        if any(k in err for k in ("GIFT_UPGRADE", "UNSUPPORTED", "NOT_ALLOWED", "FORBIDDEN")):
            return False
        # Unexpected error — fall through to method 2
    except AttributeError:
        pass  # method not in this TL layer

    # Method 2: check can_upgrade on the saved gift
    try:
        me = await client.get_me()
        peer = await client.get_input_entity(me)
        result = await _fetch_saved_gifts(client, peer, "", 100)
        for saved in result.gifts:
            if getattr(saved, "msg_id", None) == surf_msg_id:
                return bool(getattr(saved, "can_upgrade", False))
    except Exception:
        pass

    return False


async def read_surf_num(client: TelegramClient, msg_id: int) -> Optional[int]:
    """
    Fallback: re-fetch saved gifts and return the num of the now-unique gift
    identified by msg_id.
    """
    me = await client.get_me()
    peer = await client.get_input_entity(me)
    result = await _fetch_saved_gifts(client, peer, "", 100)

    for saved in result.gifts:
        if getattr(saved, "msg_id", None) != msg_id:
            continue
        gift = getattr(saved, "gift", None)
        if gift is not None:
            return getattr(gift, "num", None)

    return None
