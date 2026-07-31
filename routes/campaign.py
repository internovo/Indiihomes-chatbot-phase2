"""Campaign-specific routes that the WATI campaign flow calls back
into after a template send. Kept separate from routes/webhook.py
(which is a passive logging placeholder for future push events) and
from anything Phase 1-related - this router only ever talks to the
campaign pipeline (services/campaign_property_service.py,
services/campaign_context.py, services/notify_service.py).

POST /save-lead intentionally NOT added here - per team decision, the
campaign flow no longer writes leads to the CRM on the site-visit /
advisor branches; POST /notify-advisor (below) emails the advisor
instead.
"""
from fastapi import APIRouter

from integrations.indihomes_client import get_indihomes_client
from models.notify import NotifyAdvisorRequest, NotifyAdvisorResponse
from models.property_detail import PropertyDetailRequest, PropertyDetailResponse
from services import campaign_property_service, notify_service

router = APIRouter(tags=["campaign"])


@router.post("/property-detail", response_model=PropertyDetailResponse)
async def property_detail(payload: PropertyDetailRequest) -> PropertyDetailResponse:
    """Campaign-specific property lookup for the WATI campaign flow.

    NOT the Phase 1 recommendation/search flow - this only resolves
    the single project a campaign lead was already routed to, by
    project_code (preferred, passed by the flow or recorded at
    template-send time) or a phone-based fallback lookup."""
    client = get_indihomes_client()
    return await campaign_property_service.resolve_campaign_property(
        client, payload.phone, payload.project_code
    )


@router.post("/notify-advisor", response_model=NotifyAdvisorResponse)
async def notify_advisor(payload: NotifyAdvisorRequest) -> NotifyAdvisorResponse:
    """Fired by the WATI campaign flow when a lead clicks "Book a Site
    Visit" (both the no-slots and successfully-booked outcomes) or
    "Talk to an Advisor". Emails the advisor with the lead's details
    instead of writing to the CRM. Never raises - a failed email must
    not break the WATI flow the webhook call is part of."""
    sent = notify_service.notify_advisor(payload)
    return NotifyAdvisorResponse(sent="yes" if sent else "no")
