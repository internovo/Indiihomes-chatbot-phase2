"""One-off lead replay, for recovering leads the pipeline dropped
during an incident.

Built 3 Sep 2026 to recover the 17 leads that were never messaged
between 28 Aug and 3 Sep (expired WATI token + the project-name
resolution bug - see claude.md). Those leads cannot come back on their
own: retry_worker's queue is in-memory and was lost on restart, and
the checkpoint has advanced past their leadDate, so campaign_worker
will never fetch them again.

WHY AN ENDPOINT AND NOT scripts/reprocess_lead.py
-------------------------------------------------
That script exists and does the same job, but it runs on whoever's
laptop invokes it - and its own docstring documents the consequence:
campaign_context.remember() then writes to THAT process's memory, not
the deployed service's. The deployed service is what WATI actually
calls back on /property-detail when the customer taps the button, so
a locally-replayed lead sees blank {{project_name}} placeholders the
moment they engage. For a recovery run aimed at re-engaging exactly
the leads we already failed once, that is the wrong trade. Running
inside the deployed process makes campaign_context correct for free.

SAFETY - this endpoint sends real WhatsApp messages to real people
------------------------------------------------------------------
1. EXPLICIT ALLOW-LIST. It replays only the lead_ids named in the
   request body. There is no "replay everything since X" mode, on
   purpose: a rewind-the-checkpoint design would put mass re-messaging
   one typo away.
2. DRY RUN BY DEFAULT. Without "confirm": true it resolves each lead
   and reports what WOULD happen, sending nothing.
3. SHARED SECRET. Requires X-Admin-Secret to match ADMIN_SECRET.
   Returns 503 when ADMIN_SECRET is unset, so an unconfigured
   deployment is closed rather than open. This is the only
   authenticated endpoint in the service precisely because it is the
   only one that can message people in bulk.
4. IDEMPOTENT. It calls the same campaign_service.process_lead the
   normal cycle does, so sent_template_store still prevents any lead
   that WAS already messaged from being messaged twice, and the
   business-hours gate still applies (off-hours leads queue rather
   than send).

It deliberately does NOT touch the checkpoint.
"""
import hmac

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from config import get_settings
from integrations.indihomes_client import get_indihomes_client
from integrations.wati_client import get_wati_client
from services import campaign_service, lead_service, property_service
from utils.constants import LeadSourceCategory
from utils.logger import get_logger
from workers import campaign_worker

logger = get_logger("admin")
router = APIRouter()


class ReplayRequest(BaseModel):
    after: str = Field(description="afterDate window to fetch from, e.g. 2026-08-28T09:12:00.000Z")
    lead_ids: list[str] = Field(description="Exact CRM lead ids to replay. Nothing else is touched.")
    confirm: bool = Field(default=False, description="False (default) = dry run, sends nothing.")


def _authorize(provided: str | None) -> None:
    secret = get_settings().admin_secret
    if not secret:
        raise HTTPException(
            status_code=503,
            detail="ADMIN_SECRET is not set on this deployment - /admin/replay is disabled.",
        )
    if not provided or not hmac.compare_digest(provided, secret):
        raise HTTPException(status_code=401, detail="Bad or missing X-Admin-Secret.")


@router.post("/admin/replay")
async def replay(payload: ReplayRequest, x_admin_secret: str | None = Header(default=None)):
    _authorize(x_admin_secret)

    indihomes_client = get_indihomes_client()
    wati_client = get_wati_client()

    raw_leads = await indihomes_client.get_new_leads(payload.after)
    all_leads = lead_service.parse_leads(raw_leads)

    wanted = set(payload.lead_ids)
    leads = [lead for lead in all_leads if lead.id in wanted]
    missing = sorted(wanted - {lead.id for lead in leads})

    results = []
    for lead in leads:
        category = lead_service.classify_lead(lead)
        # Normalizes and validates the phone exactly the way the real
        # cycle does - a lead the normal pipeline would have dropped for
        # a bad number must be dropped here too, not messaged.
        usable = lead_service.filter_property_campaign_leads([lead]) or \
            lead_service.filter_generic_interest_leads([lead])
        if not usable:
            results.append({"lead_id": lead.id, "outcome": "skipped", "reason": "no usable phone number"})
            continue
        lead = usable[0]

        if not payload.confirm:
            # A dry run is a preview, so a lookup blowing up on ONE lead
            # must report that lead as unresolvable rather than 500 the
            # whole preview and hide the other 16.
            prop = None
            if category == LeadSourceCategory.PROPERTY_CAMPAIGN:
                try:
                    prop = await property_service.resolve_property(indihomes_client, lead)
                except Exception as exc:  # noqa: BLE001 - preview must never fail the batch
                    logger.warning("Dry-run resolution failed for lead %s: %s", lead.id, exc)
            results.append({
                "lead_id": lead.id, "name": lead.name, "phone": lead.phone,
                "project_name": lead.project_name, "category": category,
                "would_resolve_to": prop.project_code if prop else None,
                "outcome": "DRY RUN - nothing sent",
            })
            continue

        processor = (
            campaign_service.process_lead
            if category == LeadSourceCategory.PROPERTY_CAMPAIGN
            else campaign_service.process_generic_lead
        )
        record = await processor(lead, indihomes_client, wati_client)
        # Same bookkeeping the normal cycle does, so this lead shows up
        # in /debug/pipeline and can't be picked up twice.
        campaign_worker._record_outcome(lead, record)
        campaign_worker._processed_lead_ids.add(lead.id)
        logger.info("Replayed lead %s (%s) -> %s", lead.id, lead.project_name, record.status)
        results.append({
            "lead_id": lead.id, "name": lead.name, "phone": lead.phone,
            "project_name": lead.project_name, "outcome": record.status,
            "last_error": record.last_error,
        })

    return {
        "mode": "SENT" if payload.confirm else "dry_run",
        "requested": len(payload.lead_ids),
        "found_in_window": len(leads),
        "not_found_in_window": missing,
        "results": results,
    }
