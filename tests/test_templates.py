"""Tests for template payload construction and the campaign_service
orchestration around it (mocked WATI + IndiHomes clients, no live
calls)."""
import pytest

from models.lead import Lead
from models.property import Property
from services import campaign_service, template_service
from utils.constants import CampaignStatus


def test_build_template_payload_shape():
    lead = Lead(_id="1", phone="919876543210", lead_source="Housing.com", name="Priya")
    prop = Property(project_code="ETH-ORO-01", project_name="Ethics Orovia")
    payload = template_service.build_template_payload(lead, prop)
    assert payload["phone"] == "919876543210"
    assert payload["template_name"]
    names = {p["name"] for p in payload["parameters"]}
    assert names == {"name", "project_name"}


class FakeIndihomesClient:
    def __init__(self, project=None, raise_on_update=False):
        self._project = project
        self._raise_on_update = raise_on_update
        self.updated_with = None

    async def fetch_project(self, project_code):
        return self._project

    async def fetch_project_by_name(self, project_name):
        return self._project

    async def update_lead(self, lead_id, payload):
        if self._raise_on_update:
            raise RuntimeError("backend unavailable")
        self.updated_with = (lead_id, payload)
        return {"ok": True}


class FakeWatiClient:
    def __init__(self, raise_on_send=False):
        self._raise_on_send = raise_on_send
        self.sent = None

    async def send_template(self, phone, template_name, parameters):
        if self._raise_on_send:
            raise RuntimeError("wati unavailable")
        self.sent = (phone, template_name, parameters)
        return {"result": "success"}


@pytest.mark.asyncio
async def test_process_lead_success_path():
    lead = Lead(_id="1", phone="919876543210", lead_source="Housing.com", projectCode="ETH-ORO-01")
    indihomes = FakeIndihomesClient(project={"projectCode": "ETH-ORO-01", "projectName": "Ethics Orovia"})
    wati = FakeWatiClient()

    record = await campaign_service.process_lead(lead, indihomes, wati)

    assert record.status == CampaignStatus.TEMPLATE_SENT
    assert wati.sent[0] == "919876543210"
    assert indihomes.updated_with[0] == "1"


@pytest.mark.asyncio
async def test_process_lead_marks_retry_when_property_unresolved():
    lead = Lead(_id="1", phone="919876543210", lead_source="Housing.com")
    indihomes = FakeIndihomesClient(project=None)
    wati = FakeWatiClient()

    record = await campaign_service.process_lead(lead, indihomes, wati)

    assert record.status == CampaignStatus.RETRYING
    assert record.next_retry_at > 0


@pytest.mark.asyncio
async def test_process_lead_marks_retry_on_wati_failure():
    lead = Lead(_id="1", phone="919876543210", lead_source="Housing.com", projectCode="ETH-ORO-01")
    indihomes = FakeIndihomesClient(project={"projectCode": "ETH-ORO-01", "projectName": "Ethics Orovia"})
    wati = FakeWatiClient(raise_on_send=True)

    record = await campaign_service.process_lead(lead, indihomes, wati)

    assert record.status == CampaignStatus.RETRYING
    assert "wati unavailable" in record.last_error
