"""Advisor email notifications for the campaign flow's site-visit /
advisor branches. Sends via Brevo's HTTPS API (https://api.brevo.com) -
works identically whether this service is running locally or on
Railway, since it's a normal HTTPS POST (port 443). We deliberately do
NOT use SMTP here: Railway blocks outbound SMTP ports (25/465/587), so
a plain smtplib client would only ever work locally and fail with
"Network is unreachable" once deployed - not worth carrying two code
paths for.

If BREVO_API_KEY isn't set, send() logs the email instead of
attempting delivery - lets the rest of the pipeline (and the WATI
flow) be exercised end to end before a Brevo account/API key exists.
"""
import json
import urllib.request
import urllib.error
from typing import List

from config import get_settings
from utils.logger import get_logger

logger = get_logger("email_client")


def _normalize(emails) -> List[str]:
    """Accepts a comma-separated string (how ADVISOR_EMAILS / NOTIFY_CC
    are stored) or a list, and returns a clean list either way."""
    if isinstance(emails, str):
        emails = emails.split(",")
    return [e.strip() for e in (emails or []) if e and e.strip()]


class EmailClient:
    def __init__(self):
        self._settings = get_settings()

    def _from_email(self) -> str:
        return self._settings.email_from

    def _from_name(self) -> str:
        return self._settings.email_from_name or "Indihomes Bookings"

    @property
    def is_configured(self) -> bool:
        return bool(self._settings.brevo_api_key)

    def _post_json(self, url: str, headers: dict, payload: dict) -> bool:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return 200 <= resp.status < 300
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8")[:300]
            except Exception:
                detail = ""
            logger.error("Brevo HTTP %s: %s", e.code, detail)
            return False
        except Exception as e:  # noqa: BLE001 - a failed email must not crash the webhook
            logger.error("Brevo request failed: %s", e)
            return False

    def _send_via_brevo(self, to: List[str], cc: List[str], subject: str, body: str) -> bool:
        payload = {
            "sender": {"email": self._from_email(), "name": self._from_name()},
            "to": [{"email": a} for a in to],
            "subject": subject,
            "textContent": body,
        }
        if cc:
            payload["cc"] = [{"email": c} for c in cc]
        headers = {
            "api-key": self._settings.brevo_api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        return self._post_json("https://api.brevo.com/v3/smtp/email", headers, payload)

    def send(self, to, cc, subject: str, body: str) -> bool:
        """Sends to every address in `to` via Brevo. Never raises -
        returns False on any misconfiguration or failure so a failed
        email can't turn into a 500 on the WATI webhook call."""
        to = _normalize(to)
        cc = _normalize(cc)

        if not to:
            logger.warning("notify_advisor: no recipient configured (ADVISOR_EMAILS) - skipping send")
            return False

        if not self.is_configured:
            logger.info(
                "BREVO_API_KEY not set - logging instead of sending.\nTo: %s\nCc: %s\nSubject: %s\n%s",
                to, cc, subject, body,
            )
            return False

        if not self._from_email():
            logger.warning("EMAIL_FROM not set - cannot send")
            return False

        return self._send_via_brevo(to, cc, subject, body)


_client_singleton: EmailClient | None = None


def get_email_client() -> EmailClient:
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = EmailClient()
    return _client_singleton
