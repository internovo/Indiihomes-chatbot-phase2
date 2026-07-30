"""Tests for lead classification and filtering - the "split leads into
two tracks" logic from the plan."""
from models.lead import Lead
from services.lead_service import classify_lead_source, filter_campaign_leads, parse_leads
from utils.constants import CAMPAIGN_SOURCE, DIRECT_SOURCE


def test_classify_direct_source():
    assert classify_lead_source("Organic WhatsApp") == DIRECT_SOURCE
    assert classify_lead_source("") == DIRECT_SOURCE


def test_classify_campaign_sources():
    assert classify_lead_source("Housing.com") == CAMPAIGN_SOURCE
    assert classify_lead_source("Ethics Orovia EOI Malad W v2") == CAMPAIGN_SOURCE
    assert classify_lead_source("Ethics Orovia EOI Malad W Video v1") == CAMPAIGN_SOURCE
    assert classify_lead_source("Meta Ads - Diwali Campaign") == CAMPAIGN_SOURCE


def test_parse_leads_skips_malformed():
    raw = [
        {"_id": "1", "phone": "9876543210", "lead_source": "Housing.com"},
        {"_id": "2"},  # missing required `phone`
    ]
    leads = parse_leads(raw)
    assert len(leads) == 1
    assert leads[0].id == "1"


def test_filter_campaign_leads_drops_direct_and_bad_phone():
    leads = [
        Lead(_id="1", phone="9876543210", lead_source="Housing.com"),
        Lead(_id="2", phone="9876543211", lead_source="Organic WhatsApp"),
        Lead(_id="3", phone="", lead_source="Housing.com"),
    ]
    result = filter_campaign_leads(leads)
    assert [lead.id for lead in result] == ["1"]
    assert result[0].phone == "919876543210"
