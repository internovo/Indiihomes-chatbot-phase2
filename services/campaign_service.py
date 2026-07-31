"""Main orchestrator: coordinates lead processing, property lookup,
template creation, WATI messaging, and the CRM status update.

Two processing paths, chosen by workers/campaign_worker.py based on
lead_service.classify_lead() - data-presence based, not source-string
based (see that module's docstring for why):

  process_lead          - Property Campaign (has project_code/project_name)
                           get-new-leads -> property_service -> formatter
                           -> template_service -> wati_client -> update-leads-by-id

  process_generic_lead  - Generic Interest (name + phone only)
                           no project data at all - just sends a
                           "thanks for your interest" template. Once the
                           lead taps its Yes/Interested quick-reply
                           button, WATI triggers Phase 1's existing
                           qualification flow from its own start (see
                           the WATI-side Trigger Flow config on that
                           button, not anything in this codebase).

Both return a CampaignRecord with the same status vocabulary, so
workers/retry_worker.py can retry either one identically - it takes
the processor function as a parameter rather than hardcoding
process_lead, specifically so Generic Interest leads get the exact
same retry/backoff treatment as Property Campaign ones.
"""
from integrations.indihomes_client import IndihomesClient
from integrations.wati_client import WatiClient
from config import get_settings
from models.campaign import CampaignRecord
from models.lead import Lead
from models.notify import NotifyAdvisorRequest
from services import campaign_context, notify_service, property_service, template_service
from utils.constants import CampaignStatus
from utils.logger import get_logger

logger = get_logger("campaign_service")


async def process_lead(
    lead: Lead,
    indihomes_client: IndihomesClient,
    wati_client: WatiClient,
) -> CampaignRecord:
    """Processes a single Property Campaign lead end to end. Returns a
    CampaignRecord describing the outcome so the caller (worker or
    retry worker) can decide what to do next - it never raises, since
    one bad lead shouldn't stop a whole worker cycle."""
    record = CampaignRecord(lead_id=lead.id, phone=lead.phone)

    if not lead.project_code and not lead.project_name:
        # Defensive safety net, not an expected path: lead_service.classify_lead
        # only ever routes a lead here when project_code or project_name is
        # already present, so this should be unreachable in normal operation.
        # Kept anyway - if a future classification change or a bad direct
        # call to process_lead() ever violates that guarantee, this is what
        # stops the original silent-loss bug (3 retries over ~80 min, then
        # abandoned with nobody told) from coming back, rather than trusting
        # the invariant to hold forever.
        logger.warning(
            "Lead %s has no project_code or project_name (source=%r) - notifying advisor instead of retrying",
            lead.id, lead.lead_source,
        )
        notify_service.notify_advisor(NotifyAdvisorRequest(
            phone=lead.phone,
            name=lead.name or "",
            reason="unresolved_project",
            lead_source=lead.lead_source,
        ))
        record.status = CampaignStatus.ADVISOR_NOTIFIED
        return record

    try:
        prop = await property_service.resolve_property(indihomes_client, lead)
        if prop is None:
            record.mark_failed("property not resolved", backoff_seconds=15 * 60)
            return record
        record.status = CampaignStatus.PROPERTY_RESOLVED

        # Remembered so routes/campaign.py's POST /property-detail can
        # resolve the right project later purely from phone, even if
        # the WATI flow calls back before a project_code contact
        # attribute is set on the contact.
        campaign_context.remember(lead.phone, prop.project_code)

        payload = template_service.build_template_payload(lead, prop)
        await wati_client.send_template(payload["phone"], payload["template_name"], payload["parameters"])
        record.mark_sent()

        await indihomes_client.update_lead(lead.id, {"status": CampaignStatus.TEMPLATE_SENT})
        logger.info("Campaign template sent to lead %s for project %s", lead.id, prop.project_name)

    except Exception as exc:  # noqa: BLE001 - a failing lead must not crash the batch
        logger.error("Lead %s failed: %s", lead.id, exc)
        record.mark_failed(str(exc), backoff_seconds=5 * 60)

    return record


async def process_generic_lead(
    lead: Lead,
    indihomes_client: IndihomesClient,
    wati_client: WatiClient,
) -> CampaignRecord:
    """Processes a single Generic Interest lead (name + phone only, no
    project data - DIRECT, Meta Ads, 99 Acres, or anything else that
    doesn't carry project_code/project_name). Deliberately much
    simpler than process_lead: no property_service call, no
    campaign_context, no /property-detail relevance at all - just the
    opening template. Everything after the lead taps it is WATI's own
    flow, not this service's concern."""
    record = CampaignRecord(lead_id=lead.id, phone=lead.phone)

    try:
        settings = get_settings()
        await wati_client.send_template(
            lead.phone,
            settings.wati_generic_template_name,
            [{"name": "name", "value": lead.name or "there"}],
        )
        record.mark_sent()

        await indihomes_client.update_lead(lead.id, {"status": CampaignStatus.TEMPLATE_SENT})
        logger.info("Generic-interest template sent to lead %s (source=%r)", lead.id, lead.lead_source)

    except Exception as exc:  # noqa: BLE001 - a failing lead must not crash the batch
        logger.error("Generic lead %s failed: %s", lead.id, exc)
        record.mark_failed(str(exc), backoff_seconds=5 * 60)

    return record


async def process_batch(
    leads: list[Lead],
    indihomes_client: IndihomesClient,
    wati_client: WatiClient,
) -> list[CampaignRecord]:
    return [await process_lead(lead, indihomes_client, wati_client) for lead in leads]


async def process_generic_batch(
    leads: list[Lead],
    indihomes_client: IndihomesClient,
    wati_client: WatiClient,
) -> list[CampaignRecord]:
    return [await process_generic_lead(lead, indihomes_client, wati_client) for lead in leads]
