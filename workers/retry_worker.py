"""Retries failed WATI/API operations with exponential backoff
(5 min -> 15 min -> 1 hour), so a transient failure doesn't just lose
a lead. Kept intentionally simple: an in-memory queue is enough for a
single-instance Railway deployment - a persistent queue (Redis/DB)
would be overengineering until this actually needs to survive
restarts or run on multiple instances.

Takes the processor function (campaign_service.process_lead or
process_generic_lead) as a parameter rather than hardcoding one,
specifically so Generic Interest (Meta Ads/EOI) leads get the exact
same retry/backoff treatment as Property Campaign (Housing.com/website)
ones - same queue, same schedule, same abandonment/notification
behavior, just a different underlying send.

A lead that exhausts every retry attempt gets an advisor-notification
email before being dropped (reason="lead_abandoned") - previously it
was just marked ABANDONED and removed from the queue with nobody ever
told. campaign_service.process_lead handles the OTHER silent-loss case
(a Property Campaign lead with no project_code/project_name at all,
which never even reaches this queue - see its "unresolved_project"
notification).
"""
from typing import Awaitable, Callable

from models.campaign import CampaignRecord
from models.lead import Lead
from models.notify import NotifyAdvisorRequest
from services import notify_service
from utils.constants import MAX_RETRY_ATTEMPTS, RETRY_BACKOFF_SECONDS, CampaignStatus
from utils.logger import get_logger

logger = get_logger("retry_worker")

Processor = Callable[[Lead, object, object], Awaitable[CampaignRecord]]

# lead_id -> (Lead, CampaignRecord, processor function to retry with)
_queue: dict[str, tuple[Lead, CampaignRecord, Processor]] = {}


def queue_for_retry(lead: Lead, record: CampaignRecord, processor: Processor) -> None:
    if record.attempts > MAX_RETRY_ATTEMPTS:
        logger.error("Lead %s exceeded max retry attempts, abandoning.", lead.id)
        record.status = CampaignStatus.ABANDONED
        _queue.pop(lead.id, None)
        notify_service.notify_advisor(NotifyAdvisorRequest(
            phone=lead.phone,
            name=lead.name or "",
            project_code=lead.project_code,
            project_name=lead.project_name,
            reason="lead_abandoned",
            lead_source=lead.lead_source,
        ))
        return
    _queue[lead.id] = (lead, record, processor)


def pending_count() -> int:
    return len(_queue)


def pop_abandoned() -> list[str]:
    """Removes and returns the lead_ids of any entries marked
    ABANDONED, so cleanup_worker doesn't need to reach into this
    module's internal queue directly."""
    abandoned = [lead_id for lead_id, (_, record, _proc) in _queue.items() if record.status == CampaignStatus.ABANDONED]
    for lead_id in abandoned:
        _queue.pop(lead_id, None)
    return abandoned


async def run_cycle(indihomes_client, wati_client) -> None:
    due = [item for item in _queue.values() if item[1].is_due()]
    if not due:
        return

    logger.info("Retrying %d lead(s)", len(due))
    for lead, old_record, processor in due:
        new_record = await processor(lead, indihomes_client, wati_client)
        if new_record.status == CampaignStatus.TEMPLATE_SENT:
            _queue.pop(lead.id, None)
            logger.info("Retry succeeded for lead %s", lead.id)
        else:
            new_record.attempts = old_record.attempts + 1
            backoff_index = min(new_record.attempts - 1, len(RETRY_BACKOFF_SECONDS) - 1)
            new_record.mark_failed(new_record.last_error or "unknown error", RETRY_BACKOFF_SECONDS[backoff_index])
            queue_for_retry(lead, new_record, processor)
