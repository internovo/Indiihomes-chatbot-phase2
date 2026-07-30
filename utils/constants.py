"""Application-wide constants."""

# Lead sources that should be routed into the short "Campaign/Portal" flow
# instead of the full generic qualification funnel. Matching is
# case-insensitive substring matching against lead_source (see
# services/lead_service.classify_lead_source).
CAMPAIGN_LEAD_SOURCE_MARKERS = [
    "housing.com",
    "eoi",           # e.g. "Ethics Orovia EOI Malad W v2"
    "meta ads",
    "landing page",
]

DIRECT_SOURCE = "direct"
CAMPAIGN_SOURCE = "campaign"


class CampaignStatus:
    """Lifecycle states for a single campaign lead as it moves through
    the worker pipeline. Stored on models.campaign.CampaignRecord."""
    NEW = "new"
    PROPERTY_RESOLVED = "property_resolved"
    TEMPLATE_SENT = "template_sent"
    FAILED = "failed"
    RETRYING = "retrying"
    ABANDONED = "abandoned"


# Backoff schedule for retry_worker, in seconds (5 min, 15 min, 1 hour).
RETRY_BACKOFF_SECONDS = [5 * 60, 15 * 60, 60 * 60]
MAX_RETRY_ATTEMPTS = len(RETRY_BACKOFF_SECONDS)
