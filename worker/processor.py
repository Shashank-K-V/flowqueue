"""
processor.py — BLPOP worker with retry logic.

Architecture
------------
The worker is a standalone Python process (separate from the FastAPI
server) that runs an infinite loop:

    1. BLPOP flowqueue:jobs  (blocks until a task_id appears)
    2. Read the full job hash from Redis
    3. Mark the job as 'processing' and increment attempt counter
    4. Execute the task handler
    5a. On success → mark 'completed', store result
    5b. On failure → check attempt count:
           • attempt < max_retries → re-queue the task_id (LPUSH)
           • attempt >= max_retries → mark 'failed', store error

Multiple worker processes can run in parallel safely because:
  - Each BLPOP pops exactly one task_id from the list; two workers
    never receive the same task_id for the same enqueue event.
  - All state mutations are independent per task_id key.
  - There is no shared in-process state between workers.

Graceful shutdown
-----------------
SIGTERM (sent by Docker / Cloud Run) sets _shutdown to True.  The main
loop exits cleanly after the current BLPOP timeout cycle, without
interrupting an in-flight job.
"""

from __future__ import annotations

import json
import logging
import signal
import sys
import time
from typing import Any, Dict

import redis

# ---------------------------------------------------------------------------
# Bootstrap: make sure 'app' package is importable when this file is run
# directly (python worker/processor.py) or via docker CMD.
# ---------------------------------------------------------------------------
import os

# Add the project root (parent of 'worker/') to sys.path so `from app.x`
# works regardless of CWD.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import get_settings
from app.models import JobStatus
from app.queue import dequeue_job, get_job, update_job_status

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("flowqueue.worker")

# ---------------------------------------------------------------------------
# Graceful shutdown flag
# ---------------------------------------------------------------------------

_shutdown: bool = False


def _handle_sigterm(signum: int, frame: Any) -> None:  # noqa: ANN401
    """Set the shutdown flag so the main loop exits cleanly."""
    global _shutdown
    logger.info("SIGTERM received — finishing current job then shutting down.")
    _shutdown = True


signal.signal(signal.SIGTERM, _handle_sigterm)
signal.signal(signal.SIGINT, _handle_sigterm)   # also handle Ctrl-C in dev


# ---------------------------------------------------------------------------
# Task handlers
# ---------------------------------------------------------------------------

def handle_word_count(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deterministic word count analysis.

    Counts total words, unique words, and character count (excluding
    spaces).  Raises ValueError if 'text' is missing from the payload
    so the worker can surface a meaningful error message.
    """
    text: str = payload.get("text", "")
    if not isinstance(text, str):
        raise ValueError(f"'text' must be a string, got {type(text).__name__}")

    words = text.split()
    unique_words = set(w.lower() for w in words)

    return {
        "word_count": len(words),
        "unique_word_count": len(unique_words),
        "char_count": len(text.replace(" ", "")),
        "text_preview": text[:100] if len(text) > 100 else text,
    }


# Registry maps task_type strings to handler callables.
# Add new task types here without touching any other code.
TASK_HANDLERS = {
    "word_count": handle_word_count,
}


def execute_task(task_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Dispatch to the correct handler based on task_type.

    Raises ValueError for unknown task types so the job is marked failed
    immediately without retrying (retrying an unknown task type would
    just burn all attempts pointlessly).
    """
    handler = TASK_HANDLERS.get(task_type)
    if handler is None:
        raise ValueError(
            f"Unknown task_type '{task_type}'. "
            f"Registered types: {list(TASK_HANDLERS.keys())}"
        )
    return handler(payload)


# ---------------------------------------------------------------------------
# Core processing loop
# ---------------------------------------------------------------------------

def process_one(r: redis.Redis, settings: Any) -> bool:
    """
    Attempt to dequeue and process a single job.

    Returns True if a job was found (whether it succeeded or failed),
    False if the queue was empty (BLPOP timed out).

    Separating the logic from the infinite loop makes it trivially
    unit-testable: inject a fake Redis, call process_one(), assert state.
    """
    task_id = dequeue_job(r, timeout=settings.blpop_timeout)

    if task_id is None:
        # Queue was empty during the timeout window — nothing to do
        return False

    logger.info("Picked up task %s", task_id)

    # ------------------------------------------------------------------
    # 1. Load the job hash
    # ------------------------------------------------------------------
    job = get_job(r, task_id)

    if job is None:
        # The hash expired between LPUSH and now (very unlikely but possible
        # if TTL is very short).  Nothing we can do — skip it.
        logger.warning("Task %s hash not found — skipping.", task_id)
        return True

    task_type: str = job["task_type"]
    payload: Dict[str, Any] = job["payload"]
    current_attempts: int = job["attempts"]

    # ------------------------------------------------------------------
    # 2. Mark as processing and increment attempt counter atomically
    # ------------------------------------------------------------------
    update_job_status(
        r,
        task_id,
        JobStatus.processing,
        increment_attempts=True,
    )

    attempt_number = current_attempts + 1  # 1-based for logging
    logger.info(
        "Processing task %s (type=%s, attempt=%d/%d)",
        task_id,
        task_type,
        attempt_number,
        settings.max_retries,
    )

    # ------------------------------------------------------------------
    # 3. Execute the task handler
    # ------------------------------------------------------------------
    try:
        result = execute_task(task_type, payload)

    except Exception as exc:  # noqa: BLE001
        error_message = str(exc)
        logger.warning(
            "Task %s failed on attempt %d: %s",
            task_id,
            attempt_number,
            error_message,
        )

        if attempt_number < settings.max_retries:
            # -------------------------------------------------------------
            # Retry: re-queue the task_id.  The status stays in a transient
            # 'queued' state.  We intentionally do NOT reset the attempts
            # counter — the next worker iteration will see the current count.
            # -------------------------------------------------------------
            logger.info(
                "Re-queuing task %s (attempt %d of %d)",
                task_id,
                attempt_number,
                settings.max_retries,
            )
            update_job_status(r, task_id, JobStatus.queued, error=error_message)
            r.lpush(settings.queue_key, task_id)

        else:
            # -------------------------------------------------------------
            # Exhausted retries: mark as permanently failed
            # -------------------------------------------------------------
            logger.error(
                "Task %s permanently failed after %d attempts: %s",
                task_id,
                attempt_number,
                error_message,
            )
            update_job_status(
                r,
                task_id,
                JobStatus.failed,
                error=error_message,
            )

        return True

    # ------------------------------------------------------------------
    # 4. Success path
    # ------------------------------------------------------------------
    update_job_status(
        r,
        task_id,
        JobStatus.completed,
        result=result,
    )
    logger.info("Task %s completed successfully: %s", task_id, result)
    return True


def run_worker() -> None:
    """
    Start the worker's main event loop.

    Connects to Redis once and reuses the connection across all jobs.
    On connection failure the worker backs off for 5 seconds and retries
    so it self-heals after a Redis restart.
    """
    settings = get_settings()
    logger.info(
        "Worker starting — redis_url=%s queue_key=%s max_retries=%d",
        settings.redis_url,
        settings.queue_key,
        settings.max_retries,
    )

    r: redis.Redis | None = None

    while not _shutdown:
        # ------------------------------------------------------------------
        # Ensure we have a live Redis connection
        # ------------------------------------------------------------------
        if r is None:
            try:
                r = redis.from_url(
                    settings.redis_url,
                    decode_responses=True,
                    socket_connect_timeout=5,
                    socket_timeout=settings.blpop_timeout + 2,
                )
                r.ping()
                logger.info("Connected to Redis.")
            except redis.RedisError as exc:
                logger.error("Cannot connect to Redis: %s — retrying in 5s", exc)
                r = None
                time.sleep(5)
                continue

        # ------------------------------------------------------------------
        # Process one job (or block for blpop_timeout seconds)
        # ------------------------------------------------------------------
        try:
            process_one(r, settings)
        except redis.RedisError as exc:
            logger.error("Redis error during processing: %s — reconnecting", exc)
            try:
                r.close()
            except Exception:  # noqa: BLE001
                pass
            r = None

    logger.info("Worker shutdown complete.")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run_worker()
