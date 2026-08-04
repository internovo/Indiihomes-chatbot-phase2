"""conftest ensures the project root is importable as top-level packages
(config, models, services, ...) regardless of where pytest is invoked
from.

Also provides autouse isolation fixtures for every module that persists
to disk under state/ (campaign_context, sent_template_store,
checkpoint). Centralized here - rather than each test file needing its
own local copy - specifically because that per-file pattern already
failed once: sent_template_store and checkpoint got proper isolation
fixtures in the files that needed them, but campaign_context didn't,
and test_run_cycle_advances_checkpoint_to_newest_lead (in
test_campaign_worker.py) was silently writing real entries into the
actual state/campaign_context.json on disk every time the suite ran
(confirmed: the 919876543211 -> ETH-ORO-01 entry found there on 4 Aug
2026 matches that test's fixture data exactly). Putting the fixtures
here means a new test file gets this protection automatically, without
anyone needing to remember to add it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from services import campaign_context  # noqa: E402
from utils import checkpoint, sent_template_store  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_campaign_context(tmp_path, monkeypatch):
    """campaign_context persists to disk (state/campaign_context.json)
    as of 4 Aug 2026 - without this, any test that calls remember()
    (directly, or indirectly via process_lead()) writes into the REAL
    file on whatever machine runs the tests. Resets both the path and
    the in-memory cache/load-flag, since campaign_context.clear() alone
    only resets the in-memory side, not the disk file."""
    fake_path = tmp_path / "campaign_context.json"
    monkeypatch.setattr(campaign_context, "_CONTEXT_PATH", str(fake_path))
    monkeypatch.setattr(campaign_context, "_phone_to_project_code", {})
    monkeypatch.setattr(campaign_context, "_loaded_from_disk", False)
    yield


@pytest.fixture(autouse=True)
def isolate_sent_template_store(tmp_path, monkeypatch):
    """sent_template_store persists to disk (state/sent_templates.json)
    as of 4 Aug 2026 - same isolation requirement as campaign_context
    above. A local copy of this fixture already existed in
    test_templates.py/test_campaign_worker.py; centralizing here means
    every test file gets it, not just the ones someone remembered to
    add it to (redundant with those local fixtures where they still
    exist, which is harmless - whichever patches last just points to
    another equally-isolated tmp_path)."""
    fake_path = tmp_path / "sent_templates.json"
    monkeypatch.setattr(sent_template_store, "_SENT_TEMPLATES_PATH", str(fake_path))
    yield


@pytest.fixture(autouse=True)
def isolate_checkpoint(tmp_path, monkeypatch):
    """Same reasoning as the two fixtures above, for
    state/checkpoint.json. Centralizing this one too closes off the
    same class of gap for any future test file that touches
    campaign_worker/checkpoint without adding its own local fixture."""
    fake_path = tmp_path / "checkpoint.json"
    monkeypatch.setattr(checkpoint, "_CHECKPOINT_PATH", str(fake_path))
    yield
