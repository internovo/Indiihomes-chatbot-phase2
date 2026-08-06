"""Health endpoint for Railway health checks and monitoring."""
from fastapi import APIRouter

from business_hours import is_business_hours
from utils import pending_queue
from workers import retry_worker

router = APIRouter()


@router.get("/health")
async def health():
    return {
        "status": "ok",
        "pending_retries": retry_worker.pending_count(),
        "business_hours": "open" if is_business_hours() else "closed",
        "pending_off_hours_queue": pending_queue.pending_count(),
        "oldest_queued_at": pending_queue.oldest_queued_at(),
    }
