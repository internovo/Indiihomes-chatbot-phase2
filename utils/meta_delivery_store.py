"""Durable delivery-status ledger for campaign template sends, keyed by
lead_id:template_name (same key shape as utils/sent_template_store.py).

WHY THIS EXISTS
----------------
sent_template_store.py only tracks "did we successfully call WATI's
send API" (a 200 from /sendTemplateMessage) - that is NOT the same as
"did WhatsApp actually deliver this to the customer". A send can be
accepted by WATI/Meta and still end up undelivered - most commonly
because Meta silently blocks/restricts delivery for policy reasons
(this is the "Meta policy restricted" scenario in claude.md). The only
way to learn the real outcome is WATI's own status webhooks
(templateMessageSent / sentMessageDELIVERED / templateMessageFailed -
see support.wati.io "How to track template message delivery and
status using Wati webhooks").

So this store has three states per (lead_id, template_name):
  PENDING   - send() succeeded, no delivery/failure webhook seen yet
  DELIVERED - sentMessageDELIVERED (or READ) webhook received -
              "double ticks" in the business's own words. Terminal -
              never touched again.
  FAILED    - templateMessageFailed webhook received. This is what
              workers/meta_resend_worker.py drains at 10 AM IST the
              next day.

Stores a serialized Lead + which processing category it came from
(same _serialize_lead/to_lead shape as utils/pending_queue.py) so the
10 AM resend can rebuild the exact send payload without an extra CRM
round-trip.

CORRELATION KEY - READ THIS BEFORE TOUCHING THE MATCHING LOGIC
-----------------------------------------------------------------
A real templateMessageFailed payload was captured in production on
16 Aug 2026 (error 131049, "retry again in a few days") and it exposed
a real design mistake, not just a wrong field name: the
sentMessageDELIVERED / templateMessageFailed events carry NO phone
number and NO template name at all - only whatsappMessageId,
conversationId, ticketId. Matching by (phone, template_name), as this
store originally did, can never work for those two event types; there
is nothing on the payload to match against.

The only field that is stable across a single message's Sent ->
Delivered/Failed lifecycle is whatsappMessageId. The templateMessageSent
event DOES carry phone (waId) + templateName + whatsappMessageId - so
the fix is: capture whatsappMessageId off the Sent event (which we can
match to our PENDING record the old way, by phone+template, since it's
the one event type that still has both), store it on that record, and
then use whatsappMessageId - and ONLY that - to resolve every
subsequent Delivered/Failed event. See attach_message_id() /
mark_delivered_by_message_id() / mark_failed_by_message_id() below.

mark_delivered()/mark_failed() (by phone+template) are KEPT for
backward compatibility / as a fallback only - in practice, given the
finding above, they will essentially never successfully match a real
Delivered/Failed webhook. Do not rely on them going forward.
"""
import json
import os
from datetime import datetime, timezone
from typing import Optional

from models.lead import Lead
from utils.logger import get_logger

logger = get_logger("meta_delivery_store")

_STORE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "state",
    "meta_delivery_status.json",
)


class DeliveryStatus:
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"
    RESENT = "resent"  # terminal - the one-time 10 AM resend has already happened for this lead+template


def _key(lead_id: str, template_name: str) -> str:
    return f"{lead_id}:{template_name}"


def _read_raw() -> dict:
    try:
        with open(_STORE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {"records": {}}
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logger.warning("Meta delivery-status ledger missing or unreadable (%s) - starting fresh.", exc)
        return {"records": {}}


def _write_raw(data: dict) -> None:
    os.makedirs(os.path.dirname(_STORE_PATH), exist_ok=True)
    tmp_path = _STORE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, sort_keys=True)
    os.replace(tmp_path, _STORE_PATH)  # atomic on both POSIX and Windows


def _serialize_lead(lead: Lead) -> dict:
    """Same alias-key round-trip as pending_queue._serialize_lead, so
    to_lead() below can reconstruct an identical Lead."""
    return {
        "_id": lead.id,
        "name": lead.name,
        "phone": lead.phone,
        "leadSource": lead.lead_source,
        "projectCode": lead.project_code,
        "projectName": lead.project_name,
        "status": lead.status,
        "leadDate": lead.lead_date,
        "createdAt": lead.created_at,
    }


def to_lead(record: dict) -> Lead:
    return Lead(**record["lead"])


def record_sent(lead: Lead, template_name: str, category: str) -> None:
    """Called right after a successful wati_client.send_template() call
    in campaign_service.py. category is "property_campaign" or
    "generic_interest" (same vocabulary as utils/pending_queue.py), so
    the resend worker knows which parameter-building path to use."""
    if not lead.id or not template_name:
        logger.warning("record_sent called with lead_id=%r template_name=%r - ignoring.", lead.id, template_name)
        return
    data = _read_raw()
    records = data.setdefault("records", {})
    key = _key(lead.id, template_name)
    records[key] = {
        "lead_id": lead.id,
        "template_name": template_name,
        "category": category,
        "status": DeliveryStatus.PENDING,
        "sent_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "phone": lead.phone,
        "lead": _serialize_lead(lead),
    }
    _write_raw(data)
    logger.info("Tracking delivery status for lead_id=%s template_name=%s (PENDING)", lead.id, template_name)


def _find_by_phone_and_template(data: dict, phone: str, template_name: str) -> Optional[str]:
    """Webhooks identify the message by phone + template name, not by
    our internal lead_id - this resolves back to our record key.
    Picks the most recently sent PENDING record for that phone+template
    if there happens to be more than one (shouldn't normally happen -
    sent_template_store's idempotency check prevents a real double
    send - but a webhook arriving for a stale/duplicate record should
    still resolve to *something* sensible rather than raising)."""
    records = data.get("records", {})
    candidates = [
        r for r in records.values()
        if r.get("phone") == phone and r.get("template_name") == template_name
        and r.get("status") == DeliveryStatus.PENDING
    ]
    if not candidates:
        return None
    newest = max(candidates, key=lambda r: r.get("sent_at", ""))
    return _key(newest["lead_id"], newest["template_name"])


def mark_delivered(phone: str, template_name: str) -> bool:
    """Called from routes/webhook.py on sentMessageDELIVERED_v2 (or
    sentMessageREAD_v2, which implies delivered). Returns True if a
    matching PENDING record was found and updated."""
    data = _read_raw()
    key = _find_by_phone_and_template(data, phone, template_name)
    if key is None:
        return False
    data["records"][key]["status"] = DeliveryStatus.DELIVERED
    data["records"][key]["delivered_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    _write_raw(data)
    logger.info("Delivery confirmed (double ticks) phone=%s template_name=%s - no resend needed", phone, template_name)
    return True


def mark_failed(phone: str, template_name: str) -> bool:
    """Called from routes/webhook.py on templateMessageFailed. Returns
    True if a matching PENDING record was found and updated - this is
    what makes the entry show up for workers/meta_resend_worker.py's
    10 AM drain."""
    data = _read_raw()
    key = _find_by_phone_and_template(data, phone, template_name)
    if key is None:
        return False
    data["records"][key]["status"] = DeliveryStatus.FAILED
    data["records"][key]["failed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    _write_raw(data)
    logger.warning(
        "Meta marked template UNDELIVERED phone=%s template_name=%s - queued for 10 AM IST resend tomorrow",
        phone, template_name,
    )
    return True


def load_failed() -> list[dict]:
    """Every record currently in FAILED state - what
    workers/meta_resend_worker.py drains."""
    data = _read_raw()
    return [r for r in data.get("records", {}).values() if r.get("status") == DeliveryStatus.FAILED]


def attach_message_id(phone: str, template_name: str, whatsapp_message_id: str) -> bool:
    """Called from routes/webhook.py on templateMessageSent - the ONE
    event type that still carries phone+template, per this module's
    correlation-key note above. Stores whatsapp_message_id on the
    matching PENDING record so mark_delivered_by_message_id /
    mark_failed_by_message_id (the events that DON'T carry phone or
    template) can find it later. Returns True if a match was found."""
    if not whatsapp_message_id:
        return False
    data = _read_raw()
    key = _find_by_phone_and_template(data, phone, template_name)
    if key is None:
        return False
    data["records"][key]["whatsapp_message_id"] = whatsapp_message_id
    _write_raw(data)
    return True


def _find_by_message_id(data: dict, whatsapp_message_id: str) -> Optional[str]:
    records = data.get("records", {})
    for k, r in records.items():
        if r.get("whatsapp_message_id") == whatsapp_message_id and r.get("status") == DeliveryStatus.PENDING:
            return k
    return None


def mark_delivered_by_message_id(whatsapp_message_id: str) -> bool:
    """Called from routes/webhook.py on sentMessageDELIVERED (or READ).
    This is the real matching path - see this module's correlation-key
    note. Returns True if a matching PENDING record was found."""
    if not whatsapp_message_id:
        return False
    data = _read_raw()
    key = _find_by_message_id(data, whatsapp_message_id)
    if key is None:
        return False
    data["records"][key]["status"] = DeliveryStatus.DELIVERED
    data["records"][key]["delivered_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    _write_raw(data)
    logger.info(
        "Delivery confirmed (double ticks) whatsapp_message_id=%s (lead_id=%s template_name=%s) - no resend needed",
        whatsapp_message_id, data["records"][key]["lead_id"], data["records"][key]["template_name"],
    )
    return True


def mark_failed_by_message_id(whatsapp_message_id: str, failed_code: str = "", failed_detail: str = "") -> bool:
    """Called from routes/webhook.py on templateMessageFailed. This is
    the real matching path - see this module's correlation-key note.
    Returns True if a matching PENDING record was found and updated -
    this is what makes the entry show up for
    workers/meta_resend_worker.py's 10 AM drain."""
    if not whatsapp_message_id:
        return False
    data = _read_raw()
    key = _find_by_message_id(data, whatsapp_message_id)
    if key is None:
        return False
    data["records"][key]["status"] = DeliveryStatus.FAILED
    data["records"][key]["failed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    data["records"][key]["failed_code"] = failed_code
    data["records"][key]["failed_detail"] = failed_detail
    _write_raw(data)
    logger.warning(
        "Meta marked template UNDELIVERED whatsapp_message_id=%s (lead_id=%s template_name=%s) code=%s detail=%s - "
        "queued for 10 AM IST resend tomorrow",
        whatsapp_message_id, data["records"][key]["lead_id"], data["records"][key]["template_name"],
        failed_code, failed_detail,
    )
    return True


def mark_resent(lead_id: str, template_name: str) -> None:
    """Terminal - the one-time resend has happened. Set regardless of
    whether the resend itself succeeds or fails, so a resend is never
    attempted twice for the same lead+template (the business asked for
    ONE next-day resend, not a repeating loop)."""
    data = _read_raw()
    key = _key(lead_id, template_name)
    if key in data.get("records", {}):
        data["records"][key]["status"] = DeliveryStatus.RESENT
        data["records"][key]["resent_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        _write_raw(data)


def get_current_status(lead_id: str, template_name: str) -> Optional[str]:
    """Used by meta_resend_worker for a final re-check immediately
    before resending - a late DELIVERED webhook may have arrived
    between the queue read and the actual resend."""
    data = _read_raw()
    record = data.get("records", {}).get(_key(lead_id, template_name))
    return record.get("status") if record else None


def get_record_by_message_id(whatsapp_message_id: str) -> Optional[dict]:
    """Additive read-only helper - NOT part of the mark_delivered/mark_failed
    contract, which intentionally still just returns bool (changing that
    return type would ripple into routes/webhook.py's existing call sites
    and any tests asserting on it).

    Added for integrations/os_events_client.py's lead-events hook: unlike
    templateMessageSent, the DELIVERED/FAILED webhook payloads carry NO
    phone number at all (see this module's own correlation-key note above)
    - so after routes/webhook.py calls mark_delivered_by_message_id() /
    mark_failed_by_message_id(), it needs a SEPARATE lookup to recover the
    phone for the lead-events emit() call. Returns the full record dict
    (including "phone", "lead", "template_name") or None if not found.
    """
    if not whatsapp_message_id:
        return None
    data = _read_raw()
    for record in data.get("records", {}).values():
        if record.get("whatsapp_message_id") == whatsapp_message_id:
            return record
    return None
