"""WATI webhook receiver.

Two jobs live here now:

1. (Original, still a no-op) reserved for the future button-tap
   trigger webhook - see the docstring that used to be the whole file.

2. (New) Template delivery-status tracking, for the "resend at 10 AM
   IST next day if Meta marked it undelivered, but NOT if it was
   delivered (double ticks)" business rule - see claude.md and
   utils/meta_delivery_store.py's module docstring for the full story.

   WATI fires these events for template message statuses (see
   support.wati.io "How to track template message delivery and status
   using Wati webhooks"):
     templateMessageSent_v2   - accepted or on its way (Status: SENT)
     sentMessageDELIVERED_v2  - delivered (Status: Delivered) - "double ticks"
     sentMessageREAD_v2       - read (Status: Read) - implies delivered too
     sentMessageREPLIED_v2    - recipient replied
     templateMessageFailed    - Meta rejected/blocked delivery (Status: Failed)

   IMPORTANT - CONFIRM AGAINST A REAL PAYLOAD BEFORE TRUSTING THIS IN
   PROD: the exact JSON key names below (whatsappNumber/phone,
   templateName/template_name) are written from WATI's public docs,
   not a payload captured from this account. The raw payload is logged
   in full on every call specifically so you can pull one real
   templateMessageFailed and one real sentMessageDELIVERED_v2 event out
   of the logs, diff them against `_extract_phone`/`_extract_template_name`
   below, and adjust the key lookups if they don't match. Don't wire
   the WATI-side webhook subscription live for templateMessageFailed /
   sentMessageDELIVERED_v2 until you've done that one-time check.

   To subscribe: WATI -> Connectors -> Webhooks -> Add Webhook -> this
   route's URL -> Enabled -> select at minimum "Template Message
   Delivered" and "Template Message Failed" (Sent/Read/Replied are
   harmless to also enable but unused by this store today).
"""
from fastapi import APIRouter, Request

from utils import meta_delivery_store
from utils.logger import get_logger

logger = get_logger("webhook")

router = APIRouter()

# Event-type string WATI puts in the payload - the exact field name it
# lives under also needs confirming against a real payload (commonly
# "eventType" or "type"; trying both defensively below).
_DELIVERED_EVENTS = {"sentMessageDELIVERED_v2", "sentMessageREAD_v2"}
_FAILED_EVENTS = {"templateMessageFailed"}


def _extract_event_type(payload: dict) -> str:
    return str(payload.get("eventType") or payload.get("type") or payload.get("event") or "")


def _extract_phone(payload: dict) -> str:
    """WATI has historically used several key names for the WhatsApp
    number across different webhook/API endpoints in this same
    integration (see integrations/wati_client.py's own v1/v2 note) -
    check the common candidates defensively rather than betting on one."""
    return str(
        payload.get("waId")
        or payload.get("whatsappNumber")
        or payload.get("phone")
        or payload.get("phoneNumber")
        or ""
    ).lstrip("+")


def _extract_template_name(payload: dict) -> str:
    return str(
        payload.get("templateName")
        or payload.get("template_name")
        or payload.get("templateId")
        or ""
    )


@router.post("/webhook/wati")
async def wati_webhook(request: Request):
    payload = await request.json()
    logger.info("Received WATI webhook: %s", payload)

    event_type = _extract_event_type(payload)
    if event_type in _DELIVERED_EVENTS or event_type in _FAILED_EVENTS:
        phone = _extract_phone(payload)
        template_name = _extract_template_name(payload)
        if not phone or not template_name:
            logger.warning(
                "Delivery-status webhook (event=%s) missing phone or template_name after extraction - "
                "raw payload logged above, update routes/webhook.py's field extraction to match it.",
                event_type,
            )
        elif event_type in _DELIVERED_EVENTS:
            found = meta_delivery_store.mark_delivered(phone, template_name)
            if not found:
                logger.info(
                    "No PENDING delivery record for phone=%s template_name=%s (event=%s) - "
                    "not a campaign send we're tracking, or already resolved.",
                    phone, template_name, event_type,
                )
        else:  # failed
            meta_delivery_store.mark_failed(phone, template_name)

    # Everything else (button taps, replies, etc.) is still a no-op for
    # now beyond logging - hook up the trigger-webhook handling here
    # once that's ready (see original module docstring).
    return {"received": True}
