"""Tests for the advisor-notification email that replaced the
save-lead CRM write on the site-visit / advisor branches."""
from models.notify import NotifyAdvisorRequest
from services import notify_service


class FakeEmailClient:
    """Stand-in for integrations.email_client.EmailClient so these
    tests never touch a real provider (Resend/SendGrid/Brevo/SMTP)."""

    def __init__(self, succeed=True):
        self.succeed = succeed
        self.sent = []  # list of (to, cc, subject, body)

    def send(self, to, cc, subject, body):
        self.sent.append((to, cc, subject, body))
        return self.succeed


def test_notify_advisor_sends_with_expected_content():
    client = FakeEmailClient()
    req = NotifyAdvisorRequest(
        phone="919876543210",
        name="Test User",
        project_code="ETH-ORO-01",
        project_name="Ethics Orovia",
        reason="site_visit_booked",
        slot_label="Sat 2 Aug, 4:00 PM",
        advisor="Priya Shah",
    )

    result = notify_service.notify_advisor(req, email_client=client)

    assert result is True
    assert len(client.sent) == 1
    to, cc, subject, body = client.sent[0]
    assert "Ethics Orovia" in subject
    assert "booked a site visit" in subject
    assert "919876543210" in body
    assert "Sat 2 Aug, 4:00 PM" in body
    assert "Priya Shah" in body


def test_notify_advisor_handles_missing_project_name_gracefully():
    client = FakeEmailClient()
    req = NotifyAdvisorRequest(
        phone="919876543210",
        name="Test User",
        project_code="ETH-ORO-01",
        reason="advisor_requested",
    )

    notify_service.notify_advisor(req, email_client=client)

    _, _, subject, body = client.sent[0]
    assert "ETH-ORO-01" in subject  # falls back to project_code
    assert "wants to talk to an advisor" in subject


def test_notify_advisor_returns_false_when_send_fails():
    client = FakeEmailClient(succeed=False)
    req = NotifyAdvisorRequest(phone="919876543210", reason="site_visit_no_slots")

    result = notify_service.notify_advisor(req, email_client=client)

    assert result is False


def test_email_client_normalizes_comma_separated_recipients():
    """ADVISOR_EMAILS / NOTIFY_CC are stored as comma-separated strings
    (matching Phase 1's format) - confirm the client splits them."""
    from integrations.email_client import _normalize

    assert _normalize("a@x.com, b@x.com,c@x.com") == ["a@x.com", "b@x.com", "c@x.com"]
    assert _normalize("") == []
    assert _normalize(None) == []
    assert _normalize(["a@x.com", " b@x.com "]) == ["a@x.com", "b@x.com"]
