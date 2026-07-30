"""Light maintenance pass. Deliberately minimal: this service has no
temp files or local cache to speak of yet, so the only real job today
is dropping abandoned entries out of the retry queue so it doesn't
grow forever. Expand this only if/when the service actually
accumulates state worth cleaning (log files, on-disk cache, etc.)."""
from utils.logger import get_logger
from workers import retry_worker

logger = get_logger("cleanup_worker")


def run_cycle() -> None:
    abandoned = retry_worker.pop_abandoned()
    if abandoned:
        logger.info("Cleaned up %d abandoned retry entries", len(abandoned))
