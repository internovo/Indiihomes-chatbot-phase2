"""Single source of truth for WHERE this service's persistent state lives.

Every JSON store in this project (checkpoint, sent_templates,
pending_queue, opted_out, meta_delivery_status, campaign_context)
previously computed its own path as `<repo root>/state/<file>`, derived
from `__file__`. That works on Railway, where a Volume is mounted over
the repo's own `state/` directory - but it breaks on Azure App Service,
where the application filesystem is REPLACED on every deployment. A path
that lives inside the deployed source tree loses its contents on each
deploy, silently: the app starts fine, the checkpoint is missing, and
every lead older than `initial_lookback_hours` is skipped forever with
nothing saying so (see utils/checkpoint.py's own ERROR log for why that
failure mode is worth this much care).

Azure Files is mounted at a path OUTSIDE the source tree - `/data` - so
the state location has to be a configuration value, not something
derived from where the code happens to be sitting.

`INDIHOMES_STATE_DIR` is that configuration point:

    Azure  -> INDIHOMES_STATE_DIR=/data/phase2-state
    Local  -> unset, falls back to this repo's own state/ directory

The fallback is deliberate and load-bearing during the migration window:
with the variable unset, behaviour is byte-for-byte what it was before,
so local development needs no change and the live Railway deployment
keeps working untouched until WATI is actually cut over.

Deliberately stdlib-only, importing nothing else from this project -
same reasoning as business_hours.py. In particular it does NOT import
config: `utils/checkpoint.py` does, but state resolution must not depend
on pydantic-settings successfully loading a .env, because checkpoint's
"state didn't persist" ERROR is precisely the signal you need when
something about the deployment is broken.
"""
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_STATE_DIR = os.path.join(os.path.dirname(_HERE), "state")


def state_dir() -> str:
    """Absolute path to the directory holding this service's JSON state.

    Reads the environment on every call rather than caching, so a test
    (or a script) can change it with monkeypatch.setenv without having to
    reload modules. The stores themselves resolve their path once at
    import time - see each module's own `_*_PATH` constant, which the
    test suite monkeypatches directly.
    """
    return os.environ.get("INDIHOMES_STATE_DIR") or _DEFAULT_STATE_DIR


def state_path(filename: str) -> str:
    """Absolute path to one state file, e.g. state_path("checkpoint.json")."""
    return os.path.join(state_dir(), filename)
