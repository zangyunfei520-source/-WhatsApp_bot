"""Inbound message handling.

Ordering matters: persist and de-duplicate the inbound message first, THEN
decide whether any automatic action is allowed. Pausing the bot or marking a
customer as opted_out/blocked/inactive must never lose the customer's message.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from ai_reply import check_rate_limits, generate_reply, should_auto_reply
from config import get_config
from database import (
    customer_ai_paused, ensure_followup_schedule, get_message_by_whatsapp_id,
    get_or_create_customer, save_message, set_customer_ai_pause,
)
from bot_state import is_paused
from memory_engine import extract_and_store_memory

logger = logging.getLogger(__name__)

# Statuses that may still receive/store messages but must never get an
# automatic reply or proactive follow-up.
NO_REPLY_STATUSES = ("opted_out", "blocked", "inactive")

# Non-text inbound messages the AI cannot read. They are stored so the salesperson
# sees them, but never trigger an automatic reply.
MEDIA_TYPES = ("image", "audio", "video", "document", "sticker", "location", "contact")


async def handle_incoming_message(phone: str, body: str, msg_id: Optional[str] = None, *,
                                  message_type: str = "text", defer_reply: bool = False,
                                  batch_text: Optional[str] = None) -> dict:
    """Store an inbound message and decide whether the AI should answer.

    ``defer_reply``: the bridge is batching several quick messages from the same
    customer; store this one and wait for the last message of the batch.
    ``batch_text``: on the last message of a batch, the combined text of the
    whole batch, so the AI answers everything the customer just said.
    """
    customer = get_or_create_customer(phone)
    message_type = message_type if message_type in MEDIA_TYPES else "text"

    # 1) Persist first. Duplicate platform message ids are ignored atomically.
    #    A short content window is used only when the bridge did not supply an
    #    id, so retried webhooks cannot create duplicate history entries.
    inbound_id = save_message(
        customer["id"], "inbound", body,
        whatsapp_msg_id=msg_id,
        status="received",
        send_kind="inbound",
        dedupe_content_window_seconds=5,
        message_type=message_type,
    )
    if inbound_id is None:
        return {"action": "ignore", "reason": "duplicate", "stored": False}

    base = {"stored": True, "conversation_id": inbound_id,
            "customer_id": customer["id"]}

    # 2) Contact status only suppresses replies, not storage.
    if customer["status"] in NO_REPLY_STATUSES:
        logger.info("Inbound stored without reply: customer=%s status=%s",
                    customer["id"], customer["status"])
        return {**base, "action": "ignore", "reason": customer["status"],
                "reply_blocked_by": "contact_status"}

    # 3) Global pause suppresses automatic generation/sending, not storage.
    if is_paused():
        return {**base, "action": "ignore", "reason": "bot_paused",
                "reply_blocked_by": "global_pause"}

    # 3b) A human replied from the phone recently: AI stays silent for this customer.
    if customer_ai_paused(customer):
        return {**base, "action": "ignore", "reason": "human_takeover",
                "reply_blocked_by": "human_takeover",
                "ai_paused_until": customer.get("ai_paused_until")}

    # 4) Only active, unpaused customers enter the automatic follow-up plan.
    ensure_followup_schedule(customer["id"])

    if defer_reply:
        return {**base, "action": "ignore", "reason": "batched"}

    if message_type != "text" and not (batch_text or "").strip():
        logger.warning("客户 %s 发送了%s，AI 无法识别，需人工查看", phone, message_type)
        return {**base, "action": "ignore", "reason": "media_needs_human",
                "message_type": message_type}

    prompt_text = (batch_text or "").strip() or body
    if not should_auto_reply(prompt_text):
        return {**base, "action": "ignore", "reason": "no_reply_needed"}

    allowed, reason = check_rate_limits(customer["id"])
    if not allowed:
        return {**base, "action": "ignore", "reason": reason}

    reply = await asyncio.to_thread(generate_reply, customer, prompt_text)
    # Pause can be toggled while the LLM is generating; do not send then.
    if is_paused():
        return {**base, "action": "ignore", "reason": "bot_paused",
                "reply_blocked_by": "global_pause", "after_generation": True}

    # 5) The reply is only a pending outbound message at this point. The bridge
    #    sends it and posts /webhook/delivery; only that receipt may mark it sent.
    outbound_id = save_message(
        customer["id"], "outbound", reply,
        ai_generated=True,
        status="pending",
        send_kind="instant_reply",
    )
    await asyncio.to_thread(extract_and_store_memory, customer["id"], prompt_text, reply)
    logger.info("Reply queued for %s (conversation=%s): %s", phone, outbound_id, reply[:60])
    return {
        **base,
        "action": "reply",
        "message": reply,
        "conversation_id": outbound_id,
        "inbound_conversation_id": inbound_id,
        "delay_seconds": 3,
        "delivery_status": "pending",
    }


async def handle_outgoing_from_phone(phone: str, body: str, msg_id: Optional[str] = None,
                                     message_type: str = "text") -> dict:
    """Record a message the salesperson sent from the phone (or another linked device).

    The bridge filters out the messages it sent itself, so anything arriving here
    was written by a human. It is stored as a manual outbound message (so the AI
    sees it in context) and AI automation for that customer is paused for
    ``business.human_takeover_minutes``.
    """
    if msg_id and get_message_by_whatsapp_id(msg_id):
        return {"stored": False, "reason": "duplicate"}
    customer = get_or_create_customer(phone)
    message_type = message_type if message_type in MEDIA_TYPES else "text"
    conv_id = save_message(
        customer["id"], "outbound", body,
        whatsapp_msg_id=msg_id,
        ai_generated=False,
        status="sent",
        send_kind="manual",
        message_type="phone" if message_type == "text" else message_type,
    )
    if conv_id is None:
        return {"stored": False, "reason": "duplicate"}

    minutes = int(getattr(get_config().business, "human_takeover_minutes", 0) or 0)
    paused_until = None
    if minutes > 0:
        paused_until = set_customer_ai_pause(customer["id"], datetime.now() + timedelta(minutes=minutes))
        logger.info("人工在手机上回复了 %s，AI 对该客户暂停至 %s", phone, paused_until)
    return {"stored": True, "conversation_id": conv_id, "customer_id": customer["id"],
            "ai_paused_until": paused_until}
