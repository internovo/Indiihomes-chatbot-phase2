"""Builds and sends the advisor-notification email. Two families of
callers:

1. routes/campaign.py's POST /notify-advisor - fired by the WATI flow
   itself when a lead taps "Book a Site Visit" or "Talk to an Advisor".
2. services/campaign_service.py and workers/retry_worker.py - fired
   directly (no HTTP involved) when a campaign lead can't be
   auto-processed at all: no project_code/project_name to look up
   (reason="unresolved_project"), or every retry attempt exhausted
   (reason="lead_abandoned"). Both would otherwise fail completely
   silently - the lead just falls out of the pipeline with nobody told.

email_client is an optional parameter (same DI pattern as
property_service.resolve_property taking an indihomes client) purely
so tests can pass a fake instead of touching real SMTP/HTTPS providers.
"""
from config import get_settings
from integrations.email_client import get_email_client
from models.notify import NotifyAdvisorRequest
from utils.logger import get_logger

logger = get_logger("notify_service")

_REASON_LABELS = {
    "advisor_requested": "wants to talk to an advisor",
    "site_visit_no_slots": "wants a site visit (no slots currently available)",
    "site_visit_booked": "booked a site visit",
    "unresolved_project": "came in via a campaign source, but has no project_code or project_name to look up - needs manual follow-up",
    "lead_abandoned": "campaign lead couldn't be processed after all retries - needs manual follow-up",
    # Free-text handling (see services/campaign_intent_router.py). Both
    # fired from the NEW POST /interpret-message fallback when a lead
    # types instead of tapping a button.
    "site_visit_requested_freetext": (
        "typed a site-visit request instead of tapping a button - route "
        "manually, no automated booking path is wired for this flow yet"
    ),
    "not_interested_freetext": "typed that they're not interested",
    # Fired from POST /interpret-message's kind=="none" branch (see
    # routes/campaign.py) - free text campaign_intent_router.py couldn't
    # classify into any of its five known intents. Sibling of Phase 1's
    # needs_human logging (Indihomes-chatbot-V1/appointments_db.py); this
    # flow has no local DB of its own, so an advisor email IS the log.
    "unclassified_freetext": "typed something the bot couldn't classify - needs manual follow-up",
}


def _subject(req: NotifyAdvisorRequest) -> str:
    label = _REASON_LABELS.get(req.reason, req.reason)
    project = req.project_name or req.project_code or "a campaign lead"
    return f"[Indihomes] {req.name or req.phone} {label} - {project}"


def _body(req: NotifyAdvisorRequest) -> str:
    lines = [
        f"Lead: {req.name or '(no name)'}",
        f"Phone: {req.phone}",
        f"Project: {req.project_name or req.project_code or '(unknown)'}",
        f"Reason: {_REASON_LABELS.get(req.reason, req.reason)}",
    ]
    if req.lead_source:
        lines.append(f"Lead source: {req.lead_source}")
    if req.slot_label:
        lines.append(f"Slot: {req.slot_label}")
    if req.advisor:
        lines.append(f"Assigned advisor (WATI): {req.advisor}")
    if req.raw_message:
        lines.append(f"Lead typed: {req.raw_message!r}")
    lines.append("")
    lines.append("This lead came from the Phase 2 campaign/portal WhatsApp flow.")
    return "\n".join(lines)


def notify_advisor(req: NotifyAdvisorRequest, email_client=None) -> bool:
    settings = get_settings()
    client = email_client or get_email_client()
    subject = _subject(req)
    body = _body(req)
    sent = client.send(settings.advisor_emails, settings.notify_cc, subject, body)
    if sent:
        logger.info("Advisor notified for lead %s (%s)", req.phone, req.reason)
    else:
        logger.warning("Advisor notification NOT sent for lead %s (%s)", req.phone, req.reason)
    return sent
