"""Tiny phone -> project_code map, persisted to disk so it survives
restarts/redeploys.

Populated by services/campaign_service.py right after a lead's
property is resolved and its template is sent. This lets
/property-detail resolve the right project even when the WATI flow
calls back with just a phone number and no project_code contact
attribute set yet.

WHY PERSISTED (fixed 4 Aug 2026): the original version was in-memory
only. After any restart or Railway redeploy, ALL phone->project_code
mappings were lost. When the WATI flow then called /property-detail,
campaign_context.get_project_code() returned None, the fallback also
had nothing (no project_code attribute set on the WATI contact yet),
and the response came back found=no. WATI then sent the template with
unresolved placeholders like {{project_name}}, {{location}}, etc.

Now persisted to state/campaign_context.json using the same
atomic-write pattern as sent_template_store.py. In-memory cache is
kept as a fast-path (avoids a disk read on every /property-detail
call) - the disk file is the source of truth on cold start.

Same tradeoff as sent_template_store: the state/ directory must be
persisted across Railway deploys. Railway Volumes handle this - the
same volume that keeps state/checkpoint.json and
state/sent_templates.json keeps this file too.
"""
import json
import os

from utils.logger import get_logger

logger = get_logger("campaign_context")

_CONTEXT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "state",
    "campaign_context.json",
)

# In-memory cache - populated on first access from disk, then kept
# in sync by remember(). Never cleared in production code paths.
_phone_to_project_code: dict[str, str] = {}
_loaded_from_disk = False


def _load_from_disk() -> None:
    """Read the persisted store into memory. Called once on first use."""
    global _loaded_from_disk
    if _loaded_from_disk:
        return
    _loaded_from_disk = True
    try:
        with open(_CONTEXT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        context = data.get("context", {})
        if isinstance(context, dict):
            _phone_to_project_code.update(context)
            logger.info(
                "Loaded %d phone->project_code entries from %s",
                len(context), _CONTEXT_PATH,
            )
    except FileNotFoundError:
        logger.info("No campaign_context store found at %s - starting fresh.", _CONTEXT_PATH)
    except (json.JSONDecodeError, Exception) as exc:  # noqa: BLE001
        logger.warning("Could not load campaign_context from disk (%s) - starting fresh.", exc)


def _write_to_disk() -> None:
    """Atomically write the current in-memory map to disk."""
    os.makedirs(os.path.dirname(_CONTEXT_PATH), exist_ok=True)
    tmp_path = _CONTEXT_PATH + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({"context": dict(_phone_to_project_code)}, f, indent=4, sort_keys=True)
        os.replace(tmp_path, _CONTEXT_PATH)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not persist campaign_context to disk (%s) - in-memory only for now.", exc)


def remember(phone: str, project_code: str | None) -> None:
    _load_from_disk()
    if not phone or not project_code:
        return
    if _phone_to_project_code.get(phone) == project_code:
        # Already recorded - skip the disk write.
        return
    _phone_to_project_code[phone] = project_code
    logger.debug("Remembered project_code=%s for phone=%s", project_code, phone)
    _write_to_disk()


def get_project_code(phone: str) -> str | None:
    _load_from_disk()
    return _phone_to_project_code.get(phone)


def clear() -> None:
    """Test helper - not used by production code paths."""
    global _loaded_from_disk
    _phone_to_project_code.clear()
    _loaded_from_disk = False
