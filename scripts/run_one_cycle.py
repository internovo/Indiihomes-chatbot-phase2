"""Runs ONE real campaign_worker cycle end to end - this WILL send
WATI templates and update lead status for any campaign lead it finds,
same as the live scheduler would. Use against a staging WATI/backend
only, never point this at production credentials casually.

Usage:
    python scripts/run_one_cycle.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import checkpoint  # noqa: E402
from utils.logger import get_logger  # noqa: E402
from workers import campaign_worker  # noqa: E402

logger = get_logger("run_one_cycle")


async def main():
    before = checkpoint.get_after_date()
    logger.info("Checkpoint before cycle: %s", before)

    await campaign_worker.run_cycle()

    after = checkpoint.get_after_date()
    logger.info("Checkpoint after cycle: %s", after)
    if before == after:
        logger.info("Checkpoint did not move - no new leads were found this cycle.")


if __name__ == "__main__":
    asyncio.run(main())
