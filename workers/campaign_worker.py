"""Core background worker. Every POLL_INTERVAL_SECONDS:
  1. Load checkpoint (afterDate)
  2. GET /get-new-leads?afterDate=<checkpoint>
  3. Classify leads into Property Campaign / Generic Interest / Ignored
  4. Process each bucket (Property Campaign: property lookup + specific
     template; Generic Interest: name-only "thanks for your interest"
     template - see services/campaign_service.py for both)
  5. Track the newest lead timestamp seen (excluding anything
     suspiciously far in the future - see _newest_timestamp)
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
from datetime import datetime, timedelta, timezone

from integrations.indihomes_client import get_indihomes_client
from integrations.wati_client import get_wati_client
from services import campaign_service, lead_service
from utils import checkpoint
from utils.constants import CampaignStatus
from utils.logger import get_logger
from workers.retry_worker import queue_for_retry

logger = get_logger("campaign_worker")

# How far ahead of the current real time a lead's timestamp is allowed
# to be before it's treated as corrupted/bad data rather than genuine -
# see _newest_timestamp's docstring for why this exists.
_MAX_FUTURE_SKEW = timedelta(minutes=10)


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _newest_timestamp(leads) -> str | None:
    """Picks the max lead.timestamp across a batch, for checkpoint
    advancement. Leads without a usable timestamp are ignored rather
    than treated as the newest - a missing timestamp must never
    accidentally rewind the checkpoint.

    Also ignores (for checkpoint purposes only) any timestamp more
    than _MAX_FUTURE_SKEW ahead of the current real time. Confirmed
    4 Aug 2026: a single lead with a corrupted/mistimed timestamp
    (non-zero sub-second precision, nearly 4 hours in the future -
    looking like a bad "current time" capture somewhere upstream, not
    a real CRM-entered date) advanced the checkpoint into the future.
    Every subsequent get-new-leads call then returned empty, since
    nothing can be "after" a future date yet - silently freezing the
    entire pipeline for hours. Real leads in that window (confirmed:
    at least 3, including 2 Generic Interest) were never even fetched,
    let alone processed or retried. A single bad timestamp must never
    be able to do that again. Note: this only affects checkpoint
    advancement - the lead itself is still processed normally either
    way, just doesn't get to decide how far the checkpoint moves."""
    now = datetime.now(timezone.utc)
    valid_timestamps = []
    for lead in leads:
        if not lead.timestamp:
            continue
        parsed = _parse_ts(lead.timestamp)
        if parsed is None:
            continue
        if parsed - now > _MAX_FUTURE_SKEW:
            logger.warning(
                "Lead %s has a timestamp (%s) more than %s in the future (current time %s) - "
                "ignoring it for checkpoint purposes (likely corrupted/mistimed data, not a real "
                "future lead). The lead itself is still processed normally.",
                lead.id, lead.timestamp, _MAX_FUTURE_SKEW, now.isoformat(),
            )
            continue
        valid_timestamps.append(lead.timestamp)
    return max(valid_timestamps) if valid_timestamps else None


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
