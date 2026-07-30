"""In-memory representation of a lead's progress through the campaign
pipeline. This is intentionally not backed by a database - the source
of truth for lead state is the existing IndiHomes backend
(PATCH /update-leads-by-id/{id}); this record only tracks in-flight
retry bookkeeping for the current process lifetime."""
import time
from dataclasses import dataclass, field

from utils.constants import CampaignStatus


@dataclass
class CampaignRecord:
    lead_id: str
    phone: str
    status: str = CampaignStatus.NEW
    attempts: int = 0
    last_error: str | None = None
    next_retry_at: float = field(default_factory=lambda: 0.0)

    def mark_failed(self, error: str, backoff_seconds: int) -> None:
        self.attempts += 1
        self.last_error = error
        self.status = CampaignStatus.RETRYING
        self.next_retry_at = time.time() + backoff_seconds

    def mark_sent(self) -> None:
        self.status = CampaignStatus.TEMPLATE_SENT
        self.last_error = None

    def is_due(self) -> bool:
        return time.time() >= self.next_retry_at
