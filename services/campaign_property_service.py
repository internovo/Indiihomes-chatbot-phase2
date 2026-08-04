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
id="BII0kGNfkFaU" vs projectCode="INV_GW_552").

Both the id-then-name-then-search lookup AND the raw JSON -> Property
mapping are shared with property_service.py (resolve_raw_project,
raw_to_property) rather than duplicated here - a previous version of
this file had its own independent copy of both, which is how a real
mismatch between the two went unnoticed for a while.
"""
import re
from typing import Optional

from models.property_detail import PropertyDetailResponse
from services import campaign_context, property_service
from services.formatter import property_to_whatsapp_card
from utils.helpers import normalize_phone
from utils.logger import get_logger

logger = get_logger("campaign_property_service")

_PLACEHOLDER_RE = re.compile(r"^\s*(?:@[A-Za-z_][A-Za-z0-9_]*|\{\{\s*[A-Za-z_][A-Za-z0-9_]*\s*\}\})\s*$")


def _not_found() -> PropertyDetailResponse:
    return PropertyDetailResponse(found="no", project_confirmed="no")


def _usable_project_code(project_code: Optional[str]) -> str | None:
    if project_code is None:
        return None
    code = project_code.strip()
    if not code:
        return None
    if _PLACEHOLDER_RE.match(code):
        return None
    return code


async def resolve_campaign_property(client, phone: str, project_code: Optional[str]) -> PropertyDetailResponse:
    phone = normalize_phone(phone)
    request_code = _usable_project_code(project_code)
    context_code = campaign_context.get_project_code(phone)
    fallback_used = request_code is None and bool(context_code)
    code = request_code or context_code

    logger.info(
        "property_detail_project_code_resolution incoming_project_code=%r fallback_used=%s "
        "final_project_code=%r phone=%s",
        project_code, fallback_used, code, phone,
    )

    if not code:
        logger.warning("No project_code available for phone %s (not in request or context store)", phone)
        return _not_found()

    # `code` is very likely a projectCode/projectName-style value, not
    # the backend's internal `id` - passed as both code and name here
    # so resolve_raw_project tries it as an id first, then as a name,
    # then falls back to a fuzzy search - see that function's docstring.
    raw = await property_service.resolve_raw_project(client, code, code)

    if raw is None:
        logger.warning("Could not resolve project_code=%s for phone %s via id, name, or search", code, phone)
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


