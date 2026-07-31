"""Small reusable helpers. Nothing here should hold state."""
import re
from datetime import datetime


def normalize_phone(raw: str) -> str:
    """Strip everything but digits and ensure a country code prefix.
    Defaults to +91 (India) since that's what every lead in this
    pipeline will be."""
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        digits = "91" + digits
    return digits


def format_date(value: str | None, fmt_out: str = "%d %b %Y") -> str:
    """Best-effort date formatting for display in WhatsApp messages.
    Falls back to the raw string if it can't be parsed rather than
    raising - a badly formatted date is not worth crashing a worker
    cycle over.

    "%Y-%m" (e.g. "2028-01") covers the real backend's
    possessionStartDate field, which is month-precision only (no day) -
    formatted as month/year rather than day/month/year in that case,
    since a fabricated "01" day would misleadingly imply day-level
    precision that isn't in the source data.
    """
    if not value:
        return ""
    for fmt_in in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt_in).strftime(fmt_out)
        except ValueError:
            continue
    try:
        return datetime.strptime(value, "%Y-%m").strftime("%b %Y")
    except ValueError:
        pass
    return value


def safe_get(d: dict, *keys, default=None):
    """Chained dict.get() that never raises on a missing/None intermediate."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur
