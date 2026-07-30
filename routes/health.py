"""Health endpoint for Railway health checks and monitoring."""
from fastapi import APIRouter

from workers import retry_worker

router = APIRouter()


@router.get("/health")
async def health():
    return {
        "status": "ok",
        "pending_retries": retry_worker.pending_count(),
    }
