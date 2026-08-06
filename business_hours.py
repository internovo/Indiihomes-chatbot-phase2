"""
business_hours.py

Single source of truth for the Indihomes business-hours window
(10:00 AM - 7:00 PM IST), shared by every service that needs to gate a
reply or an outbound send to hours a human can actually back up.

This is the SAME file as Phase 1's (Indihomes-chatbot-V1/business_hours.py),
copied in unchanged - deliberately zero dependencies beyond the stdlib, so
one file works identically in both codebases. See that project's claude.md,
"Business hours gating" task section, for the full design rationale shared
by both services, and THIS project's claude.md for what's specific to the
Phase 2 Campaign Service (the persisted pending_queue + daily flush job).
"""

from datetime import datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
# String form of the same timezone, for callers that need a plain string
# rather than a ZoneInfo object - specifically APScheduler's `cron` trigger
# (see app.py's queue_flush_worker job), which accepts a timezone name
# string directly and is what the design doc's own code example uses.
IST_NAME = "Asia/Kolkata"

BUSINESS_START = time(10, 0)
BUSINESS_END = time(19, 0)


def is_business_hours(dt: Optional[datetime] = None) -> bool:
    """True if `dt` (default: now) falls within 10:00-19:00 IST, inclusive
    of both endpoints. Always converts to IST first, so callers never need
    to worry about the server's own timezone (Railway containers run UTC).

    A naive (tzinfo-less) `dt` is assumed to already be in IST - callers
    passing a naive datetime are responsible for that being true. Every
    caller in both codebases passes either None (uses now()) or an
    already-tz-aware datetime, so this hasn't been an issue in practice,
    but it's worth knowing if a new caller ever passes a naive value from
    somewhere else.
    """
    dt = dt or datetime.now(IST)
    return BUSINESS_START <= dt.astimezone(IST).time() <= BUSINESS_END


def today_ist_date(dt: Optional[datetime] = None) -> str:
    """The current IST calendar date as 'YYYY-MM-DD'."""
    dt = dt or datetime.now(IST)
    return dt.astimezone(IST).date().isoformat()


def next_business_open(dt: Optional[datetime] = None) -> datetime:
    """The next moment business hours open, in IST.

    If `dt` is already within business hours, returns `dt` unchanged -
    callers that specifically want a FUTURE open time should check
    is_business_hours() first and only call this when it's False.

    Before 10 AM -> today's 10 AM. After 7 PM -> tomorrow's 10 AM.
    """
    dt = (dt or datetime.now(IST)).astimezone(IST)
    if is_business_hours(dt):
        return dt
    open_today = dt.replace(hour=BUSINESS_START.hour, minute=BUSINESS_START.minute,
                             second=0, microsecond=0)
    if dt.time() < BUSINESS_START:
        return open_today
    return open_today + timedelta(days=1)
