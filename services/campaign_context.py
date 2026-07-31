"""Tiny in-memory phone -> project_code map.

Populated by services/campaign_service.py right after a lead's
property is resolved and its template is sent. This lets
/property-detail resolve the right project even when the WATI flow
calls back with just a phone number and no project_code contact
attribute set yet (that attribute isn't populated automatically today
- see the note in wati_client.py).

Same tradeoff as workers/retry_worker.py's queue: in-memory only, so
it does not survive a restart. Acceptable for now - a restart is
followed by campaign_worker re-polling and re-populating this on the
next cycle. The only gap is a lead that replies to its template in the
narrow window between a restart and the next poll; graduate this to a
persisted store (e.g. alongside state/checkpoint.json) if that turns
out to matter in practice.
"""
from utils.logger import get_logger

logger = get_logger("campaign_context")

_phone_to_project_code: dict[str, str] = {}


def remember(phone: str, project_code: str | None) -> None:
    if not phone or not project_code:
        return
    _phone_to_project_code[phone] = project_code
    logger.debug("Remembered project_code=%s for phone=%s", project_code, phone)


def get_project_code(phone: str) -> str | None:
    return _phone_to_project_code.get(phone)


def clear() -> None:
    """Test helper - not used by production code paths."""
    _phone_to_project_code.clear()
