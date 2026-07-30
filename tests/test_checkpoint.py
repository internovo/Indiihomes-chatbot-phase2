"""Tests for utils/checkpoint.py - uses a temp file so tests never
touch the real state/checkpoint.json."""
import json

import pytest

from utils import checkpoint


@pytest.fixture(autouse=True)
def temp_checkpoint(tmp_path, monkeypatch):
    """Points the module at a scratch file for the duration of each test."""
    fake_path = tmp_path / "checkpoint.json"
    monkeypatch.setattr(checkpoint, "_CHECKPOINT_PATH", str(fake_path))
    yield fake_path


def test_get_after_date_falls_back_when_no_checkpoint(temp_checkpoint):
    # No file exists yet at all.
    after_date = checkpoint.get_after_date()
    assert after_date.endswith("Z")
    assert "T" in after_date


def test_save_and_read_checkpoint_roundtrip(temp_checkpoint):
    checkpoint.save_checkpoint("2026-07-30T14:50:12.000Z")
    assert checkpoint.get_after_date() == "2026-07-30T14:50:12.000Z"

    with open(temp_checkpoint, encoding="utf-8") as f:
        data = json.load(f)
    assert data == {"last_processed": "2026-07-30T14:50:12.000Z"}


def test_save_checkpoint_ignores_empty_timestamp(temp_checkpoint):
    checkpoint.save_checkpoint("2026-07-30T14:50:12.000Z")
    checkpoint.save_checkpoint("")  # should be a no-op, not overwrite with blank
    assert checkpoint.get_after_date() == "2026-07-30T14:50:12.000Z"


def test_reset_checkpoint_clears_value(temp_checkpoint):
    checkpoint.save_checkpoint("2026-07-30T14:50:12.000Z")
    checkpoint.reset_checkpoint()
    # After reset, get_after_date() should fall back to the lookback
    # window again, not return the old timestamp.
    after_date = checkpoint.get_after_date()
    assert after_date != "2026-07-30T14:50:12.000Z"


def test_get_after_date_survives_corrupt_file(temp_checkpoint):
    temp_checkpoint.write_text("{not valid json", encoding="utf-8")
    after_date = checkpoint.get_after_date()  # should not raise
    assert after_date
