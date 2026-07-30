"""Reads and writes the polling checkpoint that lets campaign_worker
ask the backend for only leads newer than the last successful cycle,
instead of re-fetching (and re-filtering) the same leads every poll.

This is the ONLY module that touches state/checkpoint.json - nothing
else should read or write that file directly.
"""
import json
import os
from datetime import datetime, timedelta, timezone

from config import get_settings
from utils.logger import get_logger

logger = get_logger("checkpoint")

_CHECKPOINT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state", "checkpoint.json")


def _read_raw() -> dict:
    try:
        with open(_CHECKPOINT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logger.warning("Checkpoint file missing or unreadable (%s) - starting fresh.", exc)
        return {"last_processed": None}


def _write_raw(data: dict) -> None:
    os.makedirs(os.path.dirname(_CHECKPOINT_PATH), exist_ok=True)
    tmp_path = _CHECKPOINT_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
    os.replace(tmp_path, _CHECKPOINT_PATH)  # atomic on both POSIX and Windows


def get_after_date() -> str:
    """Returns the ISO timestamp to pass as ?afterDate=. If there's no
    checkpoint yet (first run, or after a reset), falls back to a
    lookback window rather than the beginning of time, so a fresh
    checkpoint doesn't try to pull the entire historical lead list."""
    data = _read_raw()
    last_processed = data.get("last_processed")
    if last_processed:
        return last_processed

    settings = get_settings()
    fallback = datetime.now(timezone.utc) - timedelta(hours=settings.initial_lookback_hours)
    fallback_iso = fallback.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    logger.info("No checkpoint found - defaulting afterDate to %s (last %dh)", fallback_iso, settings.initial_lookback_hours)
    return fallback_iso


def save_checkpoint(timestamp: str) -> None:
    """Persists the newest lead timestamp seen in a successful cycle.
    Only called after leads are actually processed - a failed cycle
    should not move the checkpoint forward, or failed leads would be
    silently skipped on the next poll."""
    if not timestamp:
        logger.warning("save_checkpoint called with an empty timestamp - ignoring.")
        return
    _write_raw({"last_processed": timestamp})
    logger.debug("Checkpoint saved: %s", timestamp)


def reset_checkpoint() -> None:
    """Clears the checkpoint so the next cycle falls back to the
    lookback window again. Useful for backfills / manual recovery."""
    _write_raw({"last_processed": None})
    logger.info("Checkpoint reset.")
