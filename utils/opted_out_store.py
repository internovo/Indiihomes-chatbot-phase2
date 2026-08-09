"""Durable do-not-contact list for Phase 2's OWN proactive sends.

Checked before every future campaign template send for a phone (see
services/campaign_service.py's process_lead / process_generic_lead).

Deliberately SEPARATE from Phase 1's appointments_db.opted_out table -
different codebase, different deployment, no shared database between
the two. Why Phase 2 needs its own copy of this concern (unlike most
things, which only need to live in one phase or the other): Phase 2
INITIATES contact. A phone that tells the campaign flow to stop must
not receive a future campaign send when some OTHER lead record for the
same phone shows up later (a brand new Housing.com/Meta Ads form fill
weeks after this one) - that's a scenario unique to Phase 2, since
Phase 1 never initiates contact at all (see Indihomes-chatbot-V1's own
opted_out table + its "Free-text handling" claude.md section for the
original version of this concern).

Same file-I/O pattern as utils/sent_template_store.py and
utils/pending_queue.py - atomic tmp+replace JSON, same state/
directory, same Railway Volume, nothing new to provision.
"""
import json
import os
from datetime import datetime, timezone

from utils.logger import get_logger

logger = get_logger("opted_out_store")

_OPTED_OUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "state",
    "opted_out.json",
)


def _read_raw() -> dict:
    try:
        with open(_OPTED_OUT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {"phones": {}}
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logger.warning("Opted-out store missing or unreadable (%s) - starting fresh.", exc)
        return {"phones": {}}


def _write_raw(data: dict) -> None:
    os.makedirs(os.path.dirname(_OPTED_OUT_PATH), exist_ok=True)
    tmp_path = _OPTED_OUT_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, sort_keys=True)
    os.replace(tmp_path, _OPTED_OUT_PATH)


def mark_opted_out(phone: str) -> None:
    """Idempotent - a second 'stop' just keeps the original timestamp."""
    if not phone:
        return
    data = _read_raw()
    phones = data.setdefault("phones", {})
    phones.setdefault(phone, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
    _write_raw(data)
    logger.info("Phone %s opted out of future Phase 2 campaign sends", phone)


def is_opted_out(phone: str) -> bool:
    if not phone:
        return False
    return phone in _read_raw().get("phones", {})
