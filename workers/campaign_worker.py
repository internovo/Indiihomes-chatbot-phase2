"""Core background worker. Every POLL_INTERVAL_SECONDS:
  1. Load checkpoint (afterDate)
  2. GET /get-new-leads?afterDate=<checkpoint>
  3. Filter campaign leads
  4. Process each (property lookup, template send, CRM update)
  5. Track the newest lead timestamp seen
  6. Save checkpoint - but ONLY after a successful cycle, so a bad
     cycle can't silently skip leads.

Failed leads are handed to retry_worker's in-memory queue instead of
being retried inline here, so a single slow/failing lead can't stall
the whole cycle. Failed leads do NOT block the checkpoint from
advancing past them - they're independently retried by retry_worker,
which doesn't depend on afterDate at all.
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
    # the campaign-classified subset) - Direct-source leads still
    # count as "seen" and must not be re-fetched next cycle.
    newest_seen = _newest_timestamp(leads)

    campaign_leads = lead_service.filter_campaign_leads(leads)
    logger.info(
        "%d leads since %s, %d classified as campaign/portal",
        len(leads), after_date, len(campaign_leads),
    )

    if campaign_leads:
        records = await campaign_service.process_batch(campaign_leads, indihomes_client, wati_client)

        sent = 0
        queued = 0
        for lead, record in zip(campaign_leads, records):
            if record.status == CampaignStatus.TEMPLATE_SENT:
                sent += 1
            elif record.status == CampaignStatus.RETRYING:
                queue_for_retry(lead, record)
                queued += 1

        logger.info("Cycle complete: %d sent, %d queued for retry", sent, queued)

    if newest_seen:
        checkpoint.save_checkpoint(newest_seen)
