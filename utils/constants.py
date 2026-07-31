"""Application-wide constants."""


# Sources already fully handled elsewhere - explicitly excluded
# regardless of whether they happen to carry project data. Confirmed
# 31 Jul 2026 against the lead-source diagram: "WhatsApp Bot" and
# "DIRECT" are both CRM records Phase 1's own flow writes (via
# save-lead) once a customer-initiated WhatsApp conversation ends -
# "DIRECT" specifically means the WhatsApp icon on the website, which
# counts as the customer messaging first ("Hi, I am interested in your
# property"), so WATI's Indihomes Property Assistant_v6 flow already
# triggers on it automatically. Neither can be inferred from
# project-data presence the way everything else can, since both DO
# have project data by the time they're saved to the CRM (when the
# conversation resolves one) - it's the SOURCE, not the data, that
# says "already handled." Sending either of these a Phase 2 template
# would be a redundant re-contact of someone already mid-conversation
# or already done.
IGNORED_LEAD_SOURCE_MARKERS = [
    "whatsapp bot",
    "direct",
]


class LeadSourceCategory:
    PROPERTY_CAMPAIGN = "property_campaign"   # has project_code/project_name - property-specific flow
    GENERIC_INTEREST = "generic_interest"     # name + phone only - generic "thanks for your interest" flow
    IGNORED = "ignored"                       # matches IGNORED_LEAD_SOURCE_MARKERS - untouched


class CampaignStatus:
    """Lifecycle states for a single campaign lead as it moves through
    the worker pipeline. Stored on models.campaign.CampaignRecord."""
    NEW = "new"
    PROPERTY_RESOLVED = "property_resolved"
    TEMPLATE_SENT = "template_sent"
    FAILED = "failed"
    RETRYING = "retrying"
    ABANDONED = "abandoned"
    # Kept as a defensive safety net in campaign_service.process_lead,
    # though it should now be unreachable in normal operation: a lead
    # only ever reaches process_lead (Property Campaign) when
    # lead_service.classify_lead already confirmed it has
    # project_code or project_name. Guards against a future
    # classification bug reintroducing the silent-loss bug this fixed.
    ADVISOR_NOTIFIED = "advisor_notified"


# Backoff schedule for retry_worker, in seconds (5 min, 15 min, 1 hour).
RETRY_BACKOFF_SECONDS = [5 * 60, 15 * 60, 60 * 60]
MAX_RETRY_ATTEMPTS = len(RETRY_BACKOFF_SECONDS)
