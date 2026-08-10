"""Health endpoint for Railway health checks and monitoring."""
from fastapi import APIRouter

from business_hours import is_business_hours
from config import get_settings
from integrations import lead_routing_client
from utils import pending_queue
from workers import retry_worker

router = APIRouter()


@router.get("/health")
async def health():
    settings = get_settings()
    if not lead_routing_client.is_configured():
        lead_routing_status = "NOT CONFIGURED (Phase 3 hook is a no-op until LEAD_ROUTING_URL/LEAD_ROUTING_SHARED_SECRET are set)"
    elif settings.lead_routing_dry_run:
        lead_routing_status = "configured, DRY-RUN"
    else:
        lead_routing_status = "configured, LIVE"
    return {
        "status": "ok",
        "pending_retries": retry_worker.pending_count(),
        "business_hours": "open" if is_business_hours() else "closed",
        "pending_off_hours_queue": pending_queue.pending_count(),
        "oldest_queued_at": pending_queue.oldest_queued_at(),
        "lead_routing": lead_routing_status,
    }
