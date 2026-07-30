"""Classifies incoming leads (Direct vs Campaign/Portal) and filters
out anything not ready for the campaign pipeline. This is the
implementation of the "Lead-source classification" step from the
Campaign Lead Assistant plan."""
from models.lead import Lead
from utils.constants import CAMPAIGN_LEAD_SOURCE_MARKERS, CAMPAIGN_SOURCE, DIRECT_SOURCE
from utils.helpers import normalize_phone
from utils.logger import get_logger

logger = get_logger("lead_service")


def classify_lead_source(lead_source: str) -> str:
    """Direct = organic WhatsApp message, keeps using the existing
    generic flow. Campaign/Portal = Housing.com or any Meta Ads / EOI
    landing-page source, and gets routed into the short flow."""
    source = (lead_source or "").lower()
    if any(marker in source for marker in CAMPAIGN_LEAD_SOURCE_MARKERS):
        return CAMPAIGN_SOURCE
    return DIRECT_SOURCE


def parse_leads(raw_leads: list[dict]) -> list[Lead]:
    parsed = []
    for raw in raw_leads:
        try:
            parsed.append(Lead.model_validate(raw))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skipping malformed lead %s: %s", raw.get("_id", "?"), exc)
    return parsed


def filter_campaign_leads(leads: list[Lead]) -> list[Lead]:
    """Keeps only leads that (a) classify as campaign/portal and
    (b) have a usable phone number. A missing project_code/project_name
    is NOT filtered out here - that's a data-quality problem the
    property_service fallback handles, per the plan's note that source
    parsing is inherently fragile until every campaign attaches an
    explicit projectCode."""
    result = []
    for lead in leads:
        if classify_lead_source(lead.lead_source) != CAMPAIGN_SOURCE:
            continue
        phone = normalize_phone(lead.phone)
        if not phone:
            logger.warning("Dropping lead %s - no usable phone number", lead.id)
            continue
        lead.phone = phone
        result.append(lead)
    return result
