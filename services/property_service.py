"""Resolves which property a campaign lead is asking about, and turns
the backend's raw JSON into the Property model the rest of the
pipeline works with.

raw_to_property() is the single source of truth for that JSON->Property
mapping - used here (resolve_property, the polling-cycle path with a
full Lead on hand) AND by campaign_property_service.py (the
/property-detail webhook path, phone-only). They used to duplicate
this mapping independently, which is how a field-name mismatch against
the REAL backend shape went unnoticed in one path but not the other -
see the field notes below, confirmed against live
/fetchPaginatedFilteredProjectList data on 31 Jul 2026.

resolve_raw_project() is the equivalent single source of truth for the
LOOKUP itself (id -> name -> fuzzy search), added 3 Aug 2026 for the
same reason: both callers used to run their own independent id/name
lookup, and centralizing prevents them drifting again. The third tier
(searchText) exists because a real, live project ("Ariha Opulence")
had a data-entry typo in its stored displayName ("Ariha  Opulence " -
double space, trailing space) that an exact-match name lookup can
never tolerate.

That third tier itself needed a correction the same day: the search
endpoint turned out to do plain substring matching against the raw
stored name, not fuzzy/tokenized matching - searching the full phrase
"Ariha Opulence" (normal single space) returned NOTHING, because it's
not a literal substring of the stored "Ariha  Opulence " (double
space). Searching just the first word ("Ariha") does match - but
there's a second real project, "Ariha Vincere", sharing that same
first word. Blindly taking the first search result back would risk
confidently attaching the wrong property to a lead. So the search
tries the first word (broad net) and then verifies every candidate
against a whitespace-normalized, case-insensitive comparison of the
FULL name before accepting one - narrow enough to reject "Ariha
Vincere" when looking for "Ariha Opulence", forgiving enough to accept
"Ariha  Opulence " when looking for "Ariha Opulence".
"""
import re
from typing import Any, Optional

from integrations.indihomes_client import IndihomesClient
from models.lead import Lead
from models.property import Property
from services.formatter import normalize_media_urls
from utils.helpers import format_date
from utils.logger import get_logger

logger = get_logger("property_service")


def _first_present(raw: dict, *keys: str) -> Any:
    for key in keys:
        value = raw.get(key)
        if value is not None and value != "":
            return value
    return None


def _price_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        # Real shape: {"value": 218, "unit": "Lakh"} - not a plain number/string.
        amount, unit = value.get("value"), value.get("unit", "")
        if amount in (None, ""):
            return None
        return f"{amount} {unit}".strip()
    return str(value)


def _location_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        # Real shape: {"label": "Goregaon West", "value": "goregaon west"}
        return value.get("label") or value.get("value") or None
    return str(value)


def _configurations_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        # Real shape: flatConfiguration -> ["2BHK", "3BHK", "Jodi"]
        joined = ", ".join(str(v) for v in value if v)
        return joined or None
    return str(value)


def _carpet_area_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        # Real shape: carpetSize -> {"min": 690, "max": 903, "unit": "Sq. Ft."}
        lo, hi, unit = value.get("min"), value.get("max"), value.get("unit", "")
        if lo and hi and str(lo) != str(hi):
            return f"{lo}-{hi} {unit}".strip()
        single = lo or hi
        return f"{single} {unit}".strip() if single else None
    return str(value)


def _floor_plan_url(raw: dict) -> str | None:
    direct = _first_present(raw, "floorPlanUrl", "floor_plan_url")
    if direct:
        return direct
    floor_urls = raw.get("floor_urls")
    if isinstance(floor_urls, list) and floor_urls:
        return floor_urls[0]
    inventory = raw.get("flatInventory")
    if isinstance(inventory, list) and inventory:
        for item in inventory:
            if isinstance(item, dict) and item.get("floorPlanUrl"):
                return item["floorPlanUrl"]
    return None


def _normalize_for_match(text: str) -> str:
    """Collapses any run of whitespace to a single space and lowercases,
    so "Ariha  Opulence " and "Ariha Opulence" compare equal despite a
    data-entry whitespace typo in the stored value."""
    return re.sub(r"\s+", " ", text or "").strip().lower()


def raw_to_property(raw: dict, fallback_code: str = "", fallback_name: str = "") -> Property:
    """Turns the backend's raw project JSON (from /fetchProject,
    /fetchProjectByName, or /fetchPaginatedFilteredProjectList) into a
    Property. Every helper above is defensive about the real nested
    shapes (dicts/lists) the live backend actually returns, while still
    accepting plain strings/numbers - so older/simpler fixtures and
    possible future flatter API responses both still work."""
    media_urls = normalize_media_urls(_first_present(raw, "media_urls", "mediaUrls"))
    image_url = _first_present(raw, "imageUrl", "image_url") or (media_urls[0] if media_urls else None)

    return Property(
        project_code=_first_present(raw, "projectCode", "project_code", "projectName") or fallback_code or "",
        project_name=_first_present(raw, "displayName", "display_name", "projectName", "project_name") or fallback_name or "",
        location=_location_text(_first_present(raw, "location")),
        price_range=_price_text(_first_present(raw, "startingPrice", "starting_price", "priceRange", "price_range")),
        configurations=_configurations_text(_first_present(raw, "flatConfiguration", "configurations")),
        possession_date=format_date(_first_present(raw, "possessionStartDate", "possessionDate", "possession_date")),
        carpet_area=_carpet_area_text(_first_present(raw, "carpetSize", "carpetArea", "carpet_area")),
        floor_plan_url=_floor_plan_url(raw),
        image_url=image_url,
        media_urls=media_urls,
    )


async def resolve_raw_project(client: IndihomesClient, code: Optional[str], name: Optional[str]) -> dict | None:
    """Three-tier lookup, shared by both callers below so they can't
    drift out of sync with each other again (see module docstring):

    1. fetch_project(code) - works if `code` happens to be the backend's
       real internal `id`.
    2. fetch_project_by_name(name) - works for an exact display-name match.
    3. fetch_filtered_projects({"searchText": <first word of name>}) -
       a fuzzy fallback for data-entry typos (extra whitespace) that
       step 2's exact match can't tolerate. Searches on just the first
       word (the search endpoint does literal substring matching, not
       tokenized fuzzy matching, so the full phrase can miss a
       whitespace-inconsistent stored value) and verifies every
       candidate against a whitespace-normalized comparison of the FULL
       name before accepting one - broad enough to find the typo,
       narrow enough to reject a different project sharing the same
       first word (e.g. "Ariha Vincere" when looking for "Ariha
       Opulence").
    """
    raw: dict | None = None

    if code:
        raw = await client.fetch_project(code)

    if raw is None and name:
        raw = await client.fetch_project_by_name(name)

    if raw is None and name:
        first_word = name.split()[0] if name.split() else name
        candidates = await client.fetch_filtered_projects({"searchText": first_word, "limit": 20})
        target = _normalize_for_match(name)
        for candidate in candidates:
            display = candidate.get("displayName") or candidate.get("projectName") or ""
            if _normalize_for_match(display) == target:
                raw = candidate
                logger.info(
                    "Resolved project via searchText fallback for name=%r (matched display=%r among %d candidate(s))",
                    name, display, len(candidates),
                )
                break

    return raw


async def resolve_property(client: IndihomesClient, lead: Lead) -> Property | None:
    """Prefers an explicit project_code (the reliable path the plan
    recommends every campaign eventually attach). Falls back to
    project_name / fetchProjectByName, then a verified fuzzy search,
    for campaigns that only pass a name today - or whose stored name
    has a data-quality issue an exact match can't tolerate."""
    raw = await resolve_raw_project(client, lead.project_code, lead.project_name)

    if raw is None:
        logger.warning(
            "Could not resolve a property for lead %s (project_code=%s, project_name=%s)",
            lead.id, lead.project_code, lead.project_name,
        )
        return None

    return raw_to_property(raw, fallback_code=lead.project_code or "", fallback_name=lead.project_name or "")
