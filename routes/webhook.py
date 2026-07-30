"""Reserved for future webhook callbacks - e.g. WATI notifying us that
a customer tapped a template's reply button. Not wired into the
campaign flow yet (the plan's step 4, the Template-trigger piece, is
meant to be built last per the suggested rollout order), but the route
exists so WATI has somewhere to point once that's ready."""
from fastapi import APIRouter, Request

from utils.logger import get_logger

logger = get_logger("webhook")

router = APIRouter()


@router.post("/webhook/wati")
async def wati_webhook(request: Request):
    payload = await request.json()
    logger.info("Received WATI webhook: %s", payload)
    # Intentionally a no-op for now beyond logging - hook up button-tap
    # handling here once the trigger webhook (plan step 4) is built.
    return {"received": True}
