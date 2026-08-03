"""Tests for lead classification and filtering.

Revised 31 Jul 2026 from source-string matching to data-presence
matching - see services/lead_service.py's docstring for why. Test
cases below are the actual scenarios found in real CRM data that day:
Housing.com with AND without project data, DIRECT (never has project
data, but ignored regardless per the lead-source diagram - see below),
several differently-named Meta Ads creatives, 99 Acres, and WhatsApp
Bot (has project data but must still be ignored).

IMPORTANT (3 Aug 2026): most tests below construct Lead(...) directly
with lead_source=... as a keyword argument, which works via
populate_by_name regardless of whether an alias is set - this is
exactly why the missing "leadSource" alias bug went undetected by this
whole file for so long. test_parse_leads_from_realistic_raw_dict below
is the one that actually exercises Lead.model_validate() against the
real backend's camelCase key, which is what would have caught it.
"""
from models.lead import Lead
from services.lead_service import (
    classify_lead,
    filter_generic_interest_leads,
    filter_property_campaign_leads,
    parse_leads,
)
from utils.constants import LeadSourceCategory


def test_classify_property_campaign_by_project_data_presence():
    """Any source is Property Campaign if it has project data -
    including Housing.com leads that do (not all of them do - see
    the next test)."""
    assert classify_lead(
        Lead(_id="1", phone="9876543210", lead_source="Housing.com", projectCode="ETH-ORO-01")
    ) == LeadSourceCategory.PROPERTY_CAMPAIGN
    assert classify_lead(
        Lead(_id="2", phone="9876543210", lead_source="website", projectName="Dhariwal Ashirwad Avenue")
    ) == LeadSourceCategory.PROPERTY_CAMPAIGN


def test_classify_generic_interest_when_no_project_data_regardless_of_source():
    """The actual finding: Housing.com WITHOUT project data (confirmed
    real, e.g. "Amanjit Ahluwalia") and Meta Ads creatives with wildly
    different names - none share a common substring, so none can be
    enumerated as markers. All of these must fall through to Generic
    Interest identically. DIRECT is NOT in this list - see
    test_classify_ignored_overrides_project_data_presence below,
    since it's excluded regardless of its (also absent) project data."""
    sources_with_no_project_data = [
        "Housing.com",
        "ArkadeEvoke_AD1_Video",
        "Treesourus_Ad1 -HeroExterior",
        "Agarwal Florence Lead Gen Ad1",
        "Agarwal Florence Lead Gen Video Ad1",
        "ArihaVincere_PriceFrirst_StoryAD1",
        "99 Acres",
    ]
    for source in sources_with_no_project_data:
        lead = Lead(_id="1", phone="9876543210", lead_source=source)
        assert classify_lead(lead) == LeadSourceCategory.GENERIC_INTEREST, f"failed for source={source!r}"


def test_classify_ignored_overrides_project_data_presence():
    """WhatsApp Bot and DIRECT leads both get excluded regardless of
    project data - WhatsApp Bot DOES have project data (Phase 1's own
    flow wrote it via save-lead) but must still be ignored; DIRECT
    never has project data but is excluded for the same underlying
    reason (already handled by Phase 1's own flow - "DIRECT" = the
    WhatsApp icon on the website, which counts as the customer
    messaging first). Both would otherwise risk a redundant re-contact
    of someone already mid-conversation or already done."""
    whatsapp_bot_lead = Lead(_id="1", phone="9876543210", lead_source="WhatsApp Bot", projectCode="INV_GE_901")
    assert classify_lead(whatsapp_bot_lead) == LeadSourceCategory.IGNORED

    direct_lead = Lead(_id="2", phone="9876543211", lead_source="DIRECT")
    assert classify_lead(direct_lead) == LeadSourceCategory.IGNORED


def test_parse_leads_from_realistic_raw_dict_maps_leadsource_correctly():
    """THE regression test for the 3 Aug 2026 bug: Lead had no alias
    for lead_source at all, so the real backend's camelCase
    "leadSource" key was never recognized and silently defaulted to
    "" for every real lead ever parsed - which in turn meant the
    IGNORED-category check above could never actually fire in
    production, despite being correctly implemented. Uses the REAL key
    name deliberately, unlike every Lead(...) constructor call above
    (which works via populate_by_name regardless of alias and would
    never have caught this)."""
    raw = [{
        "_id": "abc123",
        "name": "Test Person",
        "phone": "9876543210",
        "leadSource": "WhatsApp Bot",  # the real backend's actual key
        "projectCode": "INV_GE_901",
        "leadDate": "2026-08-03T09:58:00.000Z",
    }]
    leads = parse_leads(raw)
    assert len(leads) == 1
    assert leads[0].lead_source == "WhatsApp Bot"
    # And the thing that actually matters: classification now correctly
    # ignores it, end to end from raw dict through to category.
    assert classify_lead(leads[0]) == LeadSourceCategory.IGNORED


def test_parse_leads_skips_malformed():
    raw = [
        {"_id": "1", "phone": "9876543210", "leadSource": "Housing.com"},
        {"_id": "2"},  # missing required `phone`
    ]
    leads = parse_leads(raw)
    assert len(leads) == 1
    assert leads[0].id == "1"
    assert leads[0].lead_source == "Housing.com"


def test_filter_property_campaign_leads_drops_others_and_bad_phone():
    leads = [
        Lead(_id="1", phone="9876543210", lead_source="Housing.com", projectCode="ETH-ORO-01"),
        Lead(_id="2", phone="9876543211", lead_source="website", projectName="Altavia"),
        Lead(_id="3", phone="9876543212", lead_source="99 Acres"),  # no project data
        Lead(_id="4", phone="", lead_source="Housing.com", projectCode="ETH-ORO-01"),  # bad phone
        Lead(_id="5", phone="9876543213", lead_source="WhatsApp Bot", projectCode="INV_GE_901"),  # ignored
        Lead(_id="6", phone="9876543214", lead_source="DIRECT"),  # ignored
    ]
    result = filter_property_campaign_leads(leads)
    assert [lead.id for lead in result] == ["1", "2"]
    assert result[0].phone == "919876543210"


def test_filter_generic_interest_leads_keeps_only_no_project_data_and_not_ignored():
    leads = [
        Lead(_id="1", phone="9876543210", lead_source="Housing.com", projectCode="ETH-ORO-01"),
        Lead(_id="2", phone="9876543211", lead_source="DIRECT"),  # ignored, not Generic Interest
        Lead(_id="3", phone="", lead_source="Agarwal Florence Lead Gen Ad1"),  # bad phone, dropped
        Lead(_id="4", phone="9876543212", lead_source="WhatsApp Bot"),  # ignored, even without project data here
        Lead(_id="5", phone="9876543213", lead_source="99 Acres"),
    ]
    result = filter_generic_interest_leads(leads)
    assert [lead.id for lead in result] == ["5"]
