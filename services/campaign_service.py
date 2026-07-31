"""Main orchestrator: coordinates lead processing, property lookup,
template creation, WATI messaging, and the CRM status update. This is
the single place that implements the "Implementation Flow" diagram
from the blueprint:

  get-new-leads -> lead_service -> property_service -> formatter
  -> template_service -> wati_client -> update-leads-by-id
"""
from integrations.indihomes_client import IndihomesClient
from integrations.wati_client import WatiClient
from models.campaign import CampaignRecord
from models.lead import Lead
from services import campaign_context, property_service, template_service
from utils.constants import CampaignStatus
from utils.logger import get_logger

logger = get_logger("campaign_service")


async def process_lead(
    lead: Lead,
    indihomes_client: IndihomesClient,
    wati_client: WatiClient,
) -> CampaignRecord:
    """Processes a single campaign lead end to end. Returns a
    CampaignRecord describing the outcome so the caller (worker or
    retry worker) can decide what to do next - it never raises, since
    one bad lead shouldn't stop a whole worker cycle."""
    record = CampaignRecord(lead_id=lead.id, phone=lead.phone)

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


async def process_batch(
    leads: list[Lead],
    indihomes_client: IndihomesClient,
    wati_client: WatiClient,
) -> list[CampaignRecord]:
    return [await process_lead(lead, indihomes_client, wati_client) for lead in leads]
