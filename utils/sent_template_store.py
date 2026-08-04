"""Durable idempotency ledger for WATI campaign template sends.

The CRM status update is best-effort and can fail after WATI has
accepted a template send. This file is the local source of truth that
prevents a restarted worker from sending the same template to the same
lead ID again when the CRM still returns that lead as new.
"""
import json
import os
from datetime import datetime, timezone

from utils.logger import get_logger

logger = get_logger("sent_template_store")

_SENT_TEMPLATES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "state",
    "sent_templates.json",
)


def _key(lead_id: str, template_name: str) -> str:
    return f"{lead_id}:{template_name}"


def _read_raw() -> dict:
    try:
        with open(_SENT_TEMPLATES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {"sent": {}}
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logger.warning("Sent-template ledger missing or unreadable (%s) - starting fresh.", exc)
        return {"sent": {}}


def _write_raw(data: dict) -> None:
    os.makedirs(os.path.dirname(_SENT_TEMPLATES_PATH), exist_ok=True)
    tmp_path = _SENT_TEMPLATES_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, sort_keys=True)
    os.replace(tmp_path, _SENT_TEMPLATES_PATH)


def has_sent(lead_id: str, template_name: str) -> bool:
    if not lead_id or not template_name:
        return False
    sent = _read_raw().get("sent", {})
    return _key(lead_id, template_name) in sent


def mark_sent(lead_id: str, template_name: str) -> None:
    if not lead_id or not template_name:
        logger.warning("mark_sent called with lead_id=%r template_name=%r - ignoring.", lead_id, template_name)
        return
    data = _read_raw()
    sent = data.setdefault("sent", {})
    key = _key(lead_id, template_name)
    sent.setdefault(key, {
        "lead_id": lead_id,
        "template_name": template_name,
        "sent_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    })
    _write_raw(data)
    logger.info("Recorded sent campaign template lead_id=%s template_name=%s", lead_id, template_name)
