"""Persisted queue for leads that resolved successfully but arrived
off business hours, per Indihomes_Business_Hours_Gating.docx §3.3.

Same file-I/O pattern as utils/sent_template_store.py (atomic
tmp+replace JSON, same style) and stored on the same Railway Volume as
state/checkpoint.json and state/sent_templates.json - one persistent
volume already covers this, nothing new to provision.

WHY A SEPARATE QUEUE, NOT JUST retry_worker's IN-MEMORY ONE
--------------------------------------------------------------
retry_worker.py's queue is for TRANSIENT failures (network errors,
timeouts) and is deliberately in-memory with a 5min/15min/1hr backoff -
a lead sitting there for hours would exhaust its attempts and get
abandoned. An off-hours lead isn't failing; it's WAITING for something
that will happen deterministically at a known future time (business
open). That needs to survive a restart (a Railway redeploy overnight
must not lose queued leads) and needs a completely different flush
trigger (once daily, at business open - not a repeating backoff), so
it gets its own persisted store rather than overloading retry_worker's.

WHY STORE A SERIALIZED LEAD, NOT A RESOLVED Property
-------------------------------------------------------
Deliberately stores just enough to reconstruct the original Lead object
(see to_lead()), not the already-resolved property_service.Property.
The flush job re-runs campaign_service.process_lead /
process_generic_lead from scratch - the SAME functions the normal poll
cycle uses - so every existing safeguard (sent_template_store's
idempotency check, the CRM-update-after-send isolation, per-lead
exception handling) applies identically to a flushed lead as to a
freshly polled one, and a re-resolve picks up any property data that
changed overnight rather than sending stale details.
"""
import json
import os
from datetime import datetime, timezone
from typing import Optional

from models.lead import Lead
from utils.logger import get_logger

logger = get_logger("pending_queue")

_PENDING_QUEUE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "state",
    "pending_queue.json",
)


def _read_raw() -> dict:
    try:
        with open(_PENDING_QUEUE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {"queue": []}
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logger.warning("Pending off-hours queue missing or unreadable (%s) - starting fresh.", exc)
        return {"queue": []}


def _write_raw(data: dict) -> None:
    os.makedirs(os.path.dirname(_PENDING_QUEUE_PATH), exist_ok=True)
    tmp_path = _PENDING_QUEUE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, sort_keys=False)
    os.replace(tmp_path, _PENDING_QUEUE_PATH)  # atomic on both POSIX and Windows


def _serialize_lead(lead: Lead) -> dict:
    """Round-trips through the SAME alias keys the backend itself sends
    (see models.lead.Lead's Field aliases), so to_lead() below can
    reconstruct an identical Lead with a plain Lead(**dict) call."""
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


def to_lead(entry: dict) -> Lead:
    return Lead(**entry["lead"])


def enqueue(lead: Lead, category: str) -> None:
    """Adds a lead to the queue, oldest-first ordering preserved.

    Idempotent by lead_id: if this lead is already queued (e.g. the
    flush job's own defensive re-check finds it's STILL off-hours and
    re-queues it), the existing entry is updated in place rather than
    appended a second time - this both prevents duplicate entries and
    preserves the ORIGINAL queued_at, since "oldest lead first" should
    reflect when a lead first went off-hours, not when it was last
    touched.
    """
    if not lead.id:
        logger.warning("enqueue called with a lead that has no id - ignoring.")
        return
    data = _read_raw()
    queue = data.setdefault("queue", [])
    for entry in queue:
        if entry.get("lead_id") == lead.id:
            entry["lead"] = _serialize_lead(lead)
            entry["category"] = category
            _write_raw(data)
            logger.info("Re-queued lead %s (still off-hours) - queued_at unchanged", lead.id)
            return
    queue.append({
        "lead_id": lead.id,
        "category": category,
        "queued_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "lead": _serialize_lead(lead),
    })
    _write_raw(data)
    logger.info("Queued lead %s off-hours (category=%s)", lead.id, category)


def load_all() -> list[dict]:
    """Every queued entry, oldest first (insertion order == queue order,
    since enqueue() only appends new leads and updates in place for
    re-queues rather than moving them)."""
    return _read_raw().get("queue", [])


def remove_many(lead_ids: list[str]) -> None:
    """Removes the given lead_ids from the queue. Called by the flush
    job ONLY for entries that actually got sent/notified/handed to the
    retry queue this cycle - an entry that re-queued itself (still
    off-hours, see enqueue()'s docstring) must NOT be passed here, or
    it would be silently dropped instead of flushed tomorrow."""
    if not lead_ids:
        return
    data = _read_raw()
    ids = set(lead_ids)
    before = len(data.get("queue", []))
    data["queue"] = [e for e in data.get("queue", []) if e.get("lead_id") not in ids]
    removed = before - len(data["queue"])
    _write_raw(data)
    if removed:
        logger.info("Removed %d flushed lead(s) from the off-hours queue", removed)


def pending_count() -> int:
    return len(load_all())


def oldest_queued_at() -> Optional[str]:
    """For /health visibility - how long has the oldest lead been
    waiting? None if the queue is empty."""
    queue = load_all()
    return queue[0]["queued_at"] if queue else None
