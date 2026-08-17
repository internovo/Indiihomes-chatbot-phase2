"""WATI webhook receiver.

Two jobs live here now:

1. (Original, still a no-op) reserved for the future button-tap
   trigger webhook.

2. Template delivery-status tracking - resend at 10 AM IST next day if
   Meta marked a send UNDELIVERED, never if it was delivered (double
   ticks). See utils/meta_delivery_store.py's module docstring for the
   full design and the correlation-key finding below.

REAL PAYLOAD SHAPES - CAPTURED IN PRODUCTION 16 AUG 2026
-----------------------------------------------------------
Earlier versions of this file guessed field names from WATI's public
docs and got two things wrong, confirmed from real logged payloads:

1. Event names have NO "_v2" suffix in this account's actual webhooks:
   real values are "templateMessageSent", "sentMessageDELIVERED",
   "templateMessageFailed" - not the "_v2"-suffixed names WATI's docs
   implied.

2. MUCH more importantly: sentMessageDELIVERED and templateMessageFailed
   carry NO phone number and NO template name at all - only
   whatsappMessageId, conversationId, ticketId. Example real
   templateMessageFailed payload:
     {'eventType': 'templateMessageFailed', 'statusString': 'Failed',
      'failedCode': '131049', 'failedDetail': 'Message undeliverable as
      Meta has ... retry again in a few days', 'whatsappMessageId':
      'wamid.HBg...', 'conversationId': '...', 'ticketId': '...',
      'text': None, 'type': 'template', 'timestamp': '...',
      'operatorEmail': '...'}
   Only templateMessageSent carries phone (waId) + templateName +
   whatsappMessageId together. So the correlation key across a single
   message's lifecycle is whatsappMessageId, captured off the Sent
   event and used to resolve every later Delivered/Failed event - see
   meta_delivery_store.attach_message_id() and the two
   mark_*_by_message_id() functions.

To subscribe: WATI -> Connectors -> Webhooks -> Add Webhook -> this
route's URL -> Enabled -> at minimum "Template Message Sent",
"Sent Message is DELIVERED", "Template message FAILED".
"""
from fastapi import APIRouter, Request

from integrations.os_events_client import emit_best_effort as emit_os_event
from utils import meta_delivery_store
from utils.logger import get_logger

logger = get_logger("webhook")

router = APIRouter()

_SENT_EVENTS = {"templateMessageSent"}
_DELIVERED_EVENTS = {"sentMessageDELIVERED", "sentMessageREAD"}
_FAILED_EVENTS = {"templateMessageFailed"}


def _extract_event_type(payload: dict) -> str:
    return str(payload.get("eventType") or payload.get("type") or payload.get("event") or "")


def _extract_phone(payload: dict) -> str:
    """Only present on templateMessageSent in practice (see module
    docstring) - kept defensive across a few candidate keys in case
    other event types ever start carrying it."""
    return str(
        payload.get("waId")
        or payload.get("whatsappNumber")
        or payload.get("phone")
        or payload.get("phoneNumber")
        or ""
    ).lstrip("+")


def _extract_template_name(payload: dict) -> str:
    """Only present on templateMessageSent in practice - see module docstring."""
    return str(payload.get("templateName") or payload.get("template_name") or "")


def _extract_message_id(payload: dict) -> str:
    """The real correlation key across Sent -> Delivered/Failed for a
    single message - present on ALL three event types."""
    return str(payload.get("whatsappMessageId") or "")


@router.post("/webhook/wati")
async def wati_webhook(request: Request):
    payload = await request.json()
    logger.info("Received WATI webhook: %s", payload)

    event_type = _extract_event_type(payload)
    message_id = _extract_message_id(payload)

    if event_type in _SENT_EVENTS:
        phone = _extract_phone(payload)
        template_name = _extract_template_name(payload)
        if phone and template_name and message_id:
            attached = meta_delivery_store.attach_message_id(phone, template_name, message_id)
            if not attached:
                logger.info(
                    "templateMessageSent for phone=%s template_name=%s had no matching PENDING record "
                    "(not a campaign send we're tracking, or already resolved).",
                    phone, template_name,
                )
        else:
            logger.warning(
                "templateMessageSent missing phone/template_name/message_id after extraction - raw payload "
                "logged above, check routes/webhook.py's field extraction against it."
            )

    elif event_type in _DELIVERED_EVENTS:
        if message_id:
            meta_delivery_store.mark_delivered_by_message_id(message_id)
            # Lead-events: the DELIVERED/FAILED payloads carry NO phone (see
            # module docstring) - recover it from the record
            # mark_delivered_by_message_id just updated, via the additive
            # get_record_by_message_id() lookup (see meta_delivery_store.py).
            record = meta_delivery_store.get_record_by_message_id(message_id)
            if record and record.get("phone"):
                await emit_os_event(record["phone"], "delivered", {
                    "template_name": record.get("template_name"),
                }, idempotency_key=f"delivered:{message_id}")
        else:
            logger.warning("Delivered-type webhook (event=%s) had no whatsappMessageId - can't correlate.", event_type)

    elif event_type in _FAILED_EVENTS:
        failed_code = str(payload.get("failedCode") or "")
        failed_detail = str(payload.get("failedDetail") or "")
        if message_id:
            meta_delivery_store.mark_failed_by_message_id(message_id, failed_code, failed_detail)
            record = meta_delivery_store.get_record_by_message_id(message_id)
            if record and record.get("phone"):
                await emit_os_event(record["phone"], "failed", {
                    "template_name": record.get("template_name"),
                    "failed_code": failed_code, "failed_detail": failed_detail,
                }, idempotency_key=f"failed:{message_id}")
        else:
            logger.warning("Failed-type webhook (event=%s) had no whatsappMessageId - can't correlate.", event_type)

    # Everything else (button taps, replies, etc.) is still a no-op for
    # now beyond logging - hook up the trigger-webhook handling here
    # once that's ready.
    return {"received": True}
