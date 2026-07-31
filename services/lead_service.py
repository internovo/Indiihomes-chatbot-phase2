"""Classifies incoming leads into three categories and filters out
anything not ready for its respective pipeline.

Revised twice on 31 Jul 2026. First pass matched lead_source substrings
("housing.com", "eoi", "meta ads", ...) - real CRM data then showed
this doesn't hold up: Housing.com leads sometimes have no project data
at all, and Meta Ads campaign names vary too much to enumerate
("ArkadeEvoke_AD1_Video", "Treesourus_Ad1 -HeroExterior", "Agarwal
Florence Lead Gen Ad1", "99 Acres", "DIRECT" - none share a common
substring). The actual signal was never the source string - it's
whether the lead has project_code/project_name at all. The one thing
that DOES still need an explicit source check is excluding sources
Phase 1 already handled itself (see IGNORED_LEAD_SOURCE_MARKERS) -
that can't be inferred from data presence, since those leads DO have
project data by the time they're written to the CRM.
"""
from models.lead import Lead
from utils.constants import IGNORED_LEAD_SOURCE_MARKERS, LeadSourceCategory
from utils.helpers import normalize_phone
from utils.logger import get_logger

logger = get_logger("lead_service")


def classify_lead(lead: Lead) -> str:
    """PROPERTY_CAMPAIGN = has project_code or project_name, gets the
    property-specific flow, regardless of source. GENERIC_INTEREST =
    neither field present, gets a "thanks for your interest" template
    that hands off into Phase 1's flow - also regardless of source.
    IGNORED = lead_source matches something already handled elsewhere
    (checked first, since it overrides the data-presence rule)."""
    source = (lead.lead_source or "").lower()
    if any(marker in source for marker in IGNORED_LEAD_SOURCE_MARKERS):
        return LeadSourceCategory.IGNORED
    if lead.project_code or lead.project_name:
        return LeadSourceCategory.PROPERTY_CAMPAIGN
    return LeadSourceCategory.GENERIC_INTEREST


def parse_leads(raw_leads: list[dict]) -> list[Lead]:
    parsed = []
    for raw in raw_leads:
        try:
            parsed.append(Lead.model_validate(raw))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skipping malformed lead %s: %s", raw.get("_id", "?"), exc)
    return parsed


def _filter_by_category(leads: list[Lead], category: str) -> list[Lead]:
    result = []
    for lead in leads:
        if classify_lead(lead) != category:
            continue
        phone = normalize_phone(lead.phone)
        if not phone:
            logger.warning("Dropping lead %s - no usable phone number", lead.id)
            continue
        lead.phone = phone
        result.append(lead)
    return result


def filter_property_campaign_leads(leads: list[Lead]) -> list[Lead]:
    return _filter_by_category(leads, LeadSourceCategory.PROPERTY_CAMPAIGN)


def filter_generic_interest_leads(leads: list[Lead]) -> list[Lead]:
    return _filter_by_category(leads, LeadSourceCategory.GENERIC_INTEREST)
