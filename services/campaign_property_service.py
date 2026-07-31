"""Resolves the property/project for a campaign lead when the WATI
flow calls back into POST /property-detail.

Distinct from services/property_service.py: that one resolves a
property while a full Lead object is on hand during campaign_worker's
polling cycle. This one resolves from just a phone number (+ optional
project_code) supplied by the WATI flow after the fact, falling back
to the project_code campaign_context recorded when the template was
sent.

IMPORTANT: the value campaign_context stores (and whatever the WATI
flow might pass as projectCode) is prop.project_code - which per
property_service.raw_to_property() is populated from the backend's
projectCode/projectName field, NOT its `id` field. fetch_project()
needs `id` specifically (see its docstring in indihomes_client.py) -
those are different values (confirmed against real data: e.g.
id="BII0kGNfkFaU" vs projectCode="INV_GW_552"). So a plain
fetch_project(code) call will almost always come back empty here -
falls back to fetch_project_by_name(code), mirroring
property_service.resolve_property()'s two-step lookup.

Raw JSON -> Property field mapping is shared with property_service.py
(raw_to_property) rather than duplicated here, after a real mismatch
between the two was found: this file's version was guessing field
names from the plan doc rather than the live backend shape.
"""
from typing import Optional

from models.property_detail import PropertyDetailResponse
from services import campaign_context, property_service
from services.formatter import property_to_whatsapp_card
from utils.helpers import normalize_phone
from utils.logger import get_logger

logger = get_logger("campaign_property_service")


def _not_found() -> PropertyDetailResponse:
    return PropertyDetailResponse(found="no", project_confirmed="no")


async def resolve_campaign_property(client, phone: str, project_code: Optional[str]) -> PropertyDetailResponse:
    phone = normalize_phone(phone)
    code = project_code or campaign_context.get_project_code(phone)

    if not code:
        logger.warning("No project_code available for phone %s (not in request or context store)", phone)
        return _not_found()

    raw = await client.fetch_project(code)
    if raw is None:
        raw = await client.fetch_project_by_name(code)

    if raw is None:
        logger.warning("Could not resolve project_code=%s for phone %s via id or name lookup", code, phone)
        return _not_found()

    prop = property_service.raw_to_property(raw, fallback_code=code)

    return PropertyDetailResponse(
        found="yes",
        project_confirmed="yes",
        code=prop.project_code,
        project_name=prop.project_name,
        detail=property_to_whatsapp_card(prop),
        location=prop.location or "",
        price_range=prop.price_range or "",
        configurations=prop.configurations or "",
        carpet_area=prop.carpet_area or "",
        possession_date=prop.possession_date or "",
        floor_plan_url=prop.floor_plan_url or "",
        image_url=prop.image_url or "",
    )
