"""Core background worker. Every POLL_INTERVAL_SECONDS:
  1. Load checkpoint (afterDate)
  2. GET /get-new-leads?afterDate=<checkpoint>
  3. Classify leads into Property Campaign / Generic Interest / Ignored
  4. Process each bucket (Property Campaign: property lookup + specific
     template; Generic Interest: name-only "thanks for your interest"
     template - see services/campaign_service.py for both)
  5. Track the newest lead timestamp seen
  6. Save checkpoint - but ONLY after a successful cycle, so a bad
     cycle can't silently skip leads.

Failed leads are handed to retry_worker's in-memory queue instead of
being retried inline here, so a single slow/failing lead can't stall
the whole cycle. Failed leads do NOT block the checkpoint from
advancing past them - they're independently retried by retry_worker,
which doesn't depend on afterDate at all. Both lead categories share
the exact same retry queue and backoff schedule - retry_worker just
gets told which processor function to retry each one with.

Property Campaign leads with no project_code/project_name at all skip
the retry queue entirely and go straight to CampaignStatus.ADVISOR_NOTIFIED
(see campaign_service.process_lead) - retrying wouldn't help, since the
CRM data itself has nothing to search on. This should be rare now that
EOI/Meta Ads leads (which never have project data) are classified as
Generic Interest instead and never reach process_lead at all.
"""
from integrations.indihomes_client import get_indihomes_client
from integrations.wati_client import get_wati_client
from services import campaign_service, lead_service
from utils import checkpoint
from utils.constants import CampaignStatus
from utils.logger import get_logger
from workers.retry_worker import queue_for_retry

logger = get_logger("campaign_worker")


def _newest_timestamp(leads) -> str | None:
    """Picks the max lead.timestamp across a batch. Leads without a
    usable timestamp are ignored rather than treated as the newest -
    a missing timestamp must never accidentally rewind the checkpoint."""
    timestamps = [lead.timestamp for lead in leads if lead.timestamp]
    return max(timestamps) if timestamps else None


def _summarize_and_queue(records, leads, processor, indihomes_client=None, wati_client=None):
    """Shared bookkeeping for either lead category: counts outcomes and
    hands RETRYING ones to retry_worker with the right processor to
    retry them with. Returns (sent, queued, notified) counts."""
    sent = queued = notified = 0
    for lead, record in zip(leads, records):
        if record.status == CampaignStatus.TEMPLATE_SENT:
            sent += 1
        elif record.status == CampaignStatus.RETRYING:
            queue_for_retry(lead, record, processor)
            queued += 1
        elif record.status == CampaignStatus.ADVISOR_NOTIFIED:
            notified += 1
    return sent, queued, notified


async def run_cycle() -> None:
    indihomes_client = get_indihomes_client()
    wati_client = get_wati_client()

    after_date = checkpoint.get_after_date()
    raw_leads = await indihomes_client.get_new_leads(after_date)
    if not raw_leads:
        logger.debug("No new leads since %s.", after_date)
        return

    leads = lead_service.parse_leads(raw_leads)
    if not leads:
        return

    # Advance the checkpoint against the FULL fetched batch (not just
    # the classified subsets) - Ignored-category leads still count as
    # "seen" and must not be re-fetched next cycle.
    newest_seen = _newest_timestamp(leads)

    property_leads = lead_service.filter_property_campaign_leads(leads)
    generic_leads = lead_service.filter_generic_interest_leads(leads)
    logger.info(
        "%d leads since %s: %d Property Campaign, %d Generic Interest",
        len(leads), after_date, len(property_leads), len(generic_leads),
    )

    if property_leads:
        records = await campaign_service.process_batch(property_leads, indihomes_client, wati_client)
        sent, queued, notified = _summarize_and_queue(records, property_leads, campaign_service.process_lead)
        logger.info(
            "Property Campaign cycle: %d sent, %d queued for retry, %d advisor-notified (no project data)",
            sent, queued, notified,
        )

    if generic_leads:
        generic_records = await campaign_service.process_generic_batch(generic_leads, indihomes_client, wati_client)
        sent, queued, _ = _summarize_and_queue(generic_records, generic_leads, campaign_service.process_generic_lead)
        logger.info("Generic Interest cycle: %d sent, %d queued for retry", sent, queued)

    if newest_seen:
        checkpoint.save_checkpoint(newest_seen)
