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
    # ERROR, not INFO, as of 3 Sep 2026. On a first-ever boot this is
    # harmless. On a service that has been running for weeks it means
    # state/checkpoint.json VANISHED (the Railway Volume isn't mounted,
    # or the container's disk is ephemeral) - and the consequence is
    # SILENT LEAD LOSS: every lead older than the lookback window is
    # skipped forever, with nothing in the logs saying so. That is
    # indistinguishable, from the outside, from "the CRM just went
    # quiet". It must be impossible to miss in a log search.
    logger.error(
        "NO CHECKPOINT FILE at %s - falling back to afterDate=%s (last %dh). If this service has run "
        "before, state/ did not persist and every lead older than that window is being SKIPPED. "
        "Check that the Railway Volume is mounted at the state/ directory.",
        _CHECKPOINT_PATH, fallback_iso, settings.initial_lookback_hours,
    )
    return fallback_iso


def peek() -> str | None:
    """The stored checkpoint as-is (None if there isn't one), without the
    lookback fallback get_after_date() applies. For /health and debugging
    only - workers should call get_after_date()."""
    return _read_raw().get("last_processed")


def save_checkpoint(timestamp: str) -> None:
    """Persists the newest lead timestamp seen in a successful cycle.
    Only called after leads are actually processed - a failed cycle
    should not move the checkpoint forward, or failed leads would be
    silently skipped on the next poll."""
    if not timestamp:
        logger.warning("save_checkpoint called with an empty timestamp - ignoring.")
        return
    previous = _read_raw().get("last_processed")
    _write_raw({"last_processed": timestamp})
    # INFO, not DEBUG, as of 3 Sep 2026: the checkpoint's movement is the
    # single most useful line for reconstructing "which leads did this
    # service actually see" after the fact, and it only fires on cycles
    # that found leads (a few times a day), so it costs nothing.
    logger.info("Checkpoint moved: %s -> %s", previous, timestamp)


def reset_checkpoint() -> None:
    """Clears the checkpoint so the next cycle falls back to the
    lookback window again. Useful for backfills / manual recovery."""
    _write_raw({"last_processed": None})
    logger.info("Checkpoint reset.")
