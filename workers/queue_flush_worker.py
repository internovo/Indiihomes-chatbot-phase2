"""Daily flush of leads queued off-hours by the business-hours gate in
services/campaign_service.py (see utils/pending_queue.py and
Indihomes_Business_Hours_Gating.docx §3.3).

Runs once a day, scheduled at business open by app.py's `queue_flush_worker`
job (settings.queue_flush_hour_ist / queue_flush_minute_ist, 10:00 AM IST
by default - see claude.md, "Business hours gating", for why 10:00 AM was
chosen over the design doc's own internally-contradictory "9:00 AM" text).

Drains pending_queue.json OLDEST LEAD FIRST (list order == queue order),
re-running the SAME campaign_service.process_lead / process_generic_lead
functions the normal poll cycle uses - not a separate send path - so every
existing safeguard (the sent_template_store idempotency check, the
CRM-update-after-send isolation, per-lead exception handling) applies
identically to a flushed lead as to a freshly polled one. Per-lead failure
isolation: one lead's exception (already handled INSIDE process_lead /
process_generic_lead, which never raises) must never stop the rest of the
queue from draining.
"""
from integrations.indihomes_client import get_indihomes_client
from integrations.wati_client import get_wati_client
from services import campaign_service
from utils import pending_queue
from utils.constants import CampaignStatus
from utils.logger import get_logger
from workers.retry_worker import queue_for_retry

logger = get_logger("queue_flush_worker")


async def run_cycle() -> None:
    entries = pending_queue.load_all()
    if not entries:
        return

    logger.info("Flushing %d lead(s) queued off-hours", len(entries))
    indihomes_client = get_indihomes_client()
    wati_client = get_wati_client()

    flushed_ids: list[str] = []
    sent = queued_retry = notified = still_off_hours = 0

    for entry in entries:
        lead = pending_queue.to_lead(entry)
        processor = (
            campaign_service.process_lead
            if entry.get("category") == "property_campaign"
            else campaign_service.process_generic_lead
        )
        record = await processor(lead, indihomes_client, wati_client)

        if record.status == CampaignStatus.QUEUED_OFF_HOURS:
            # Should be unreachable in normal operation - this job only
            # ever runs once business hours have opened, so
            # is_business_hours() inside the processor should read True.
            # Defensive: if it somehow re-queues (e.g. the flush job's
            # schedule and BUSINESS_START have drifted out of sync after
            # a config change), leave the entry in pending_queue rather
            # than removing it below - losing a lead silently here would
            # be exactly the bug this whole feature exists to prevent.
            still_off_hours += 1
            logger.warning(
                "Lead %s still shows QUEUED_OFF_HOURS during a flush run - leaving it queued. "
                "Check that queue_flush_hour_ist/minute_ist matches business_hours.BUSINESS_START.",
                entry.get("lead_id"),
            )
            continue

        flushed_ids.append(entry["lead_id"])

        if record.status == CampaignStatus.TEMPLATE_SENT:
            sent += 1
        elif record.status == CampaignStatus.ADVISOR_NOTIFIED:
            notified += 1
        elif record.status == CampaignStatus.RETRYING:
            # A transient failure during the flush itself (e.g. WATI
            # briefly down at 10:00 AM) gets the NORMAL retry/backoff
            # treatment via retry_worker - it does not go back into
            # pending_queue, which would only be drained again tomorrow.
            queue_for_retry(lead, record, processor)
            queued_retry += 1

    pending_queue.remove_many(flushed_ids)
    logger.info(
        "Off-hours flush complete: %d sent, %d advisor-notified, %d queued for retry, "
        "%d still off-hours (left queued), %d removed from queue",
        sent, notified, queued_retry, still_off_hours, len(flushed_ids),
    )
