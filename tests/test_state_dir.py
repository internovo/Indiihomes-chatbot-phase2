"""Tests for utils/state_dir.py - the single choke point deciding WHERE
this service's persistent JSON state is written.

Why this file exists at all: the failure it guards against is silent. If
INDIHOMES_STATE_DIR is ignored or mis-resolved on Azure, the app still
starts, /health still returns ok, and the only symptom is that
checkpoint.json quietly vanishes on every deploy - which means every lead
older than initial_lookback_hours is skipped forever with nothing saying
so. That exact class of outage already cost this project five days once
(see claude.md, "campaign sends went silent for 5 days"). A structural
test is cheap insurance against re-introducing it.
"""
import os

import pytest

from services import campaign_context
from utils import (
    checkpoint,
    meta_delivery_store,
    opted_out_store,
    pending_queue,
    sent_template_store,
)
from utils.state_dir import state_dir, state_path

# Captured at IMPORT time, deliberately. conftest.py installs autouse
# fixtures that monkeypatch these same constants to a tmp_path for every
# test (correctly - they stop the suite writing into the real state/
# files). Those fixtures run at test time, not at collection, so reading
# the constants here is the only way to see their real, as-deployed
# values from inside the suite.
_AS_DEPLOYED = {
    "checkpoint.json": checkpoint._CHECKPOINT_PATH,
    "sent_templates.json": sent_template_store._SENT_TEMPLATES_PATH,
    "pending_queue.json": pending_queue._PENDING_QUEUE_PATH,
    "opted_out.json": opted_out_store._OPTED_OUT_PATH,
    "meta_delivery_status.json": meta_delivery_store._STORE_PATH,
    "campaign_context.json": campaign_context._CONTEXT_PATH,
}


def test_falls_back_to_repo_state_dir_when_env_unset(monkeypatch):
    """Unset env var must behave exactly as the project always did, so
    local development and the live Railway deployment are unaffected
    during the migration window."""
    monkeypatch.delenv("INDIHOMES_STATE_DIR", raising=False)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert state_dir() == os.path.join(repo_root, "state")


def test_env_var_overrides_the_default(monkeypatch):
    monkeypatch.setenv("INDIHOMES_STATE_DIR", "/data/phase2-state")
    assert state_dir() == "/data/phase2-state"
    assert state_path("checkpoint.json") == os.path.join(
        "/data/phase2-state", "checkpoint.json"
    )


def test_blank_env_var_is_treated_as_unset(monkeypatch):
    """An App Service setting left empty (or a .env line with nothing
    after the '=') must fall back rather than resolve to the filesystem
    root. The `or` in state_dir() is what makes this true, and it is
    exactly the kind of thing someone later "simplifies" into
    os.environ.get(name, default) - which would not."""
    monkeypatch.setenv("INDIHOMES_STATE_DIR", "")
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert state_dir() == os.path.join(repo_root, "state")


@pytest.mark.parametrize("filename", sorted(_AS_DEPLOYED))
def test_every_store_resolves_under_one_state_dir(filename):
    """All six stores must sit in the SAME directory, so exactly one
    Azure Files share needs mounting and there is only one thing to get
    wrong.

    A NEW store added later that computes its own path from __file__
    would not be caught here - add it to _AS_DEPLOYED when you add it to
    the codebase."""
    path = _AS_DEPLOYED[filename]
    assert os.path.basename(path) == filename
    assert os.path.dirname(path) == state_dir()


def test_all_six_stores_are_accounted_for():
    """Guards the guard: if someone trims _AS_DEPLOYED down, the
    parametrized test above silently stops covering what it claims to."""
    assert len(_AS_DEPLOYED) == 6
