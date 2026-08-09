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
from models.interpret import InterpretMessageRequest, InterpretMessageResponse
from models.notify import NotifyAdvisorRequest, NotifyAdvisorResponse
from models.property_detail import PropertyDetailRequest, PropertyDetailResponse
from services import campaign_intent_router, campaign_property_service, notify_service
from utils import opted_out_store
from utils.helpers import normalize_phone
from utils.logger import get_logger

router = APIRouter(tags=["campaign"])
logger = get_logger("routes.campaign")

from services import campaign_context

@router.post("/debug/context")
async def debug_set_context():
    campaign_context.remember(
        "919876543210",
        "INV_GW_552",
    )
    return {
        "status": "stored"
    }


@router.get("/debug/context/{phone}")
async def debug_get_context(phone: str):
    return {
        "phone": phone,
        "project_code": campaign_context.get_project_code(phone)
    }


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


@router.post("/interpret-message", response_model=InterpretMessageResponse)
async def interpret_message(payload: InterpretMessageRequest) -> InterpretMessageResponse:
    """Free-text fallback for the campaign flow's "What would you like
    to do next?" node (and any other button node in this flow that
    gets wired to it). Sibling of Phase 1's POST /interpret-message.

    Built after a real production gap: a Property Campaign lead typed
    free text at this exact node and had nowhere to go - this flow had
    no equivalent of Phase 1's intent_router.py at all. See
    services/campaign_intent_router.py for the classifier and its
    IMPORTANT honesty note: the phrase lists there are inferred, not
    yet grounded in a captured real transcript for this flow. This
    endpoint logs every classification outcome (including "none") at
    INFO specifically so those logs can be used to true up the phrase
    lists later, the same way Phase 1's were refined after Megha's
    transcript.

    WATI wiring required to actually reach this (not done as part of
    this change - no campaign flow export was available to patch
    programmatically the way Phase 1's was): wire this flow's
    "What would you like to do next?" Buttons node's default/no-match
    path to call this endpoint, the same way Phase 1's
    main_buttons-next was wired to its own /interpret-message - see
    Indihomes-chatbot-V1/claude.md, "Required WATI wiring", for the
    exact pattern (default path -> webhook -> condition on is_global).
    """
    phone = normalize_phone(payload.phone)
    text = (payload.message or "").strip()

    intent = campaign_intent_router.classify(text)
    kind = intent.get("intent", "none")
    logger.info(
        "campaign_interpret_message phone=%s intent=%s message=%r",
        phone, kind, text,
    )

    if kind == "none":
        # Genuinely unclassifiable free text - campaign_intent_router.py has no
        # phrase match for it. Previously this returned handled="no" with an
        # EMPTY reply_text and did nothing else: WATI's is_global=="no" branch
        # falls through to whatever local retry copy that node already has
        # (same convention as Phase 1 - see Indihomes-chatbot-V1/app.py's
        # _run_global_intent), so the customer was never shown a blank
        # message, but NOBODY was ever told this lead got stuck. Unlike
        # Phase 1, this flow has no local SQLite of its own to log a
        # needs_human row into, so an advisor email IS the log here - see
        # notify_service.py's "unclassified_freetext" reason.
        #
        # Best-effort, matching every other notify_advisor call site in this
        # file: notify_advisor() already swallows its own errors and returns
        # a bool, so this can never turn into a 500 on the WATI webhook call.
        notify_service.notify_advisor(NotifyAdvisorRequest(
            phone=phone, name=payload.name or "",
            project_code=payload.project_code, project_name=payload.project_name,
            reason="unclassified_freetext", raw_message=text,
        ))
        return InterpretMessageResponse(intent="none", is_global="no", handled="no")

    if kind == "stop":
        opted_out_store.mark_opted_out(phone)
        return InterpretMessageResponse(
            intent="stop", is_global="yes", handled="yes",
            reply_text="You won't hear from us again. Take care!",
        )

    if kind == "talk_to_advisor":
        sent = notify_service.notify_advisor(NotifyAdvisorRequest(
            phone=phone, name=payload.name or "",
            project_code=payload.project_code, project_name=payload.project_name,
            reason="advisor_requested",
        ))
        reply = ("Perfect! An Indihomes advisor will contact you shortly." if sent
                 else "Noted - an advisor will follow up with you shortly.")
        return InterpretMessageResponse(intent=kind, is_global="yes", handled="yes", reply_text=reply)

    if kind == "site_visit":
        # No automated slot-listing/booking path exists in this codebase's
        # routes/ as of this writing - hand off to a human rather than
        # guessing at a booking mechanism. See this function's docstring
        # and campaign_intent_router.py's module docstring.
        notify_service.notify_advisor(NotifyAdvisorRequest(
            phone=phone, name=payload.name or "",
            project_code=payload.project_code, project_name=payload.project_name,
            reason="site_visit_requested_freetext",
        ))
        return InterpretMessageResponse(
            intent=kind, is_global="yes", handled="yes",
            reply_text="Great - one of our advisors will reach out shortly to help you schedule a site visit.",
        )

    if kind == "not_interested":
        notify_service.notify_advisor(NotifyAdvisorRequest(
            phone=phone, name=payload.name or "",
            project_code=payload.project_code, project_name=payload.project_name,
            reason="not_interested_freetext",
        ))
        return InterpretMessageResponse(
            intent=kind, is_global="yes", handled="yes",
            reply_text="No problem at all - thanks for letting us know, and feel free to reach out anytime.",
        )

    if kind == "show_details":
        client = get_indihomes_client()
        detail = await campaign_property_service.resolve_campaign_property(
            client, phone, payload.project_code,
        )
        reply = detail.detail if detail.found == "yes" else (
            "Sorry, I couldn't pull up those details again right now - "
            "one of our advisors will follow up with you shortly."
        )
        if detail.found != "yes":
            notify_service.notify_advisor(NotifyAdvisorRequest(
                phone=phone, name=payload.name or "",
                project_code=payload.project_code, project_name=payload.project_name,
                reason="unresolved_project",
            ))
        return InterpretMessageResponse(intent=kind, is_global="yes", handled="yes", reply_text=reply)

    return InterpretMessageResponse(intent="none", is_global="no", handled="no")
