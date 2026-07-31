"""Builds and sends the advisor-notification email that replaces the
old save-lead CRM write for the campaign flow's site-visit and
advisor-handoff branches (team decision - see routes/campaign.py).

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
    if req.slot_label:
        lines.append(f"Slot: {req.slot_label}")
    if req.advisor:
        lines.append(f"Assigned advisor (WATI): {req.advisor}")
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
