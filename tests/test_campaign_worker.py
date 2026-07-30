"""Tests for the checkpoint-aware campaign_worker.run_cycle flow:
loads checkpoint -> fetches with afterDate -> processes -> advances
checkpoint using the newest lead timestamp in the batch."""
import pytest

from models.lead import Lead
from utils import checkpoint
from utils.constants import CampaignStatus
from workers import campaign_worker, retry_worker


@pytest.fixture(autouse=True)
def temp_checkpoint(tmp_path, monkeypatch):
    fake_path = tmp_path / "checkpoint.json"
    monkeypatch.setattr(checkpoint, "_CHECKPOINT_PATH", str(fake_path))
    yield fake_path


@pytest.fixture(autouse=True)
def clear_retry_queue():
    retry_worker._queue.clear()
    yield
    retry_worker._queue.clear()


class FakeIndihomesClient:
    def __init__(self, leads, project=None):
        self._leads = leads
        self._project = project
        self.requested_after_date = None
        self.updated_leads = []

    async def get_new_leads(self, after_date):
        self.requested_after_date = after_date
        return self._leads

    async def fetch_project(self, project_id):
        return self._project

    async def fetch_project_by_name(self, project_name):
        return self._project

    async def update_lead(self, lead_id, payload):
        self.updated_leads.append((lead_id, payload))
        return {"ok": True}


class FakeWatiClient:
    async def send_template(self, phone, template_name, parameters):
        return {"result": "success"}


@pytest.fixture(autouse=True)
def patch_clients(monkeypatch):
    """Lets each test inject its own fake clients via
    monkeypatch on the getters campaign_worker actually calls."""
    holder = {}

    def _set(indihomes=None, wati=None):
        if indihomes is not None:
            monkeypatch.setattr("workers.campaign_worker.get_indihomes_client", lambda: indihomes)
        if wati is not None:
            monkeypatch.setattr("workers.campaign_worker.get_wati_client", lambda: wati)
        holder["indihomes"] = indihomes
        holder["wati"] = wati

    return _set


@pytest.mark.asyncio
async def test_run_cycle_passes_checkpoint_as_after_date(patch_clients):
    checkpoint.save_checkpoint("2026-07-30T10:00:00.000Z")
    indihomes = FakeIndihomesClient(leads=[])
    patch_clients(indihomes=indihomes, wati=FakeWatiClient())

    await campaign_worker.run_cycle()

    assert indihomes.requested_after_date == "2026-07-30T10:00:00.000Z"


@pytest.mark.asyncio
async def test_run_cycle_advances_checkpoint_to_newest_lead(patch_clients):
    leads = [
        {"_id": "1", "phone": "9876543210", "lead_source": "Direct", "leadDate": "2026-07-30T11:00:00.000Z"},
        {"_id": "2", "phone": "9876543211", "lead_source": "Housing.com", "projectCode": "ETH-ORO-01",
         "leadDate": "2026-07-30T12:30:00.000Z"},
    ]
    indihomes = FakeIndihomesClient(leads=leads, project={"projectCode": "ETH-ORO-01", "projectName": "Ethics Orovia"})
    patch_clients(indihomes=indihomes, wati=FakeWatiClient())

    await campaign_worker.run_cycle()

    assert checkpoint.get_after_date() == "2026-07-30T12:30:00.000Z"
    # Direct-source lead should not be sent a template / updated.
    assert len(indihomes.updated_leads) == 1
    assert indihomes.updated_leads[0][0] == "2"


@pytest.mark.asyncio
async def test_run_cycle_does_not_advance_checkpoint_on_empty_batch(patch_clients):
    checkpoint.save_checkpoint("2026-07-30T10:00:00.000Z")
    indihomes = FakeIndihomesClient(leads=[])
    patch_clients(indihomes=indihomes, wati=FakeWatiClient())

    await campaign_worker.run_cycle()

    assert checkpoint.get_after_date() == "2026-07-30T10:00:00.000Z"


@pytest.mark.asyncio
async def test_run_cycle_queues_failed_lead_for_retry_but_still_advances_checkpoint(patch_clients):
    leads = [
        {"_id": "1", "phone": "9876543210", "lead_source": "Housing.com", "projectCode": "MISSING",
         "leadDate": "2026-07-30T13:00:00.000Z"},
    ]
    indihomes = FakeIndihomesClient(leads=leads, project=None)  # property never resolves -> failure
    patch_clients(indihomes=indihomes, wati=FakeWatiClient())

    await campaign_worker.run_cycle()

    assert checkpoint.get_after_date() == "2026-07-30T13:00:00.000Z"
    assert retry_worker.pending_count() == 1
