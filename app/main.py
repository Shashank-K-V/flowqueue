"""
main.py — FastAPI application entry point for FlowQueue.

Exposes three endpoints:

  POST /tasks           — Enqueue a new job; returns task_id immediately.
  GET  /tasks/{task_id} — Poll for job status / result.
  GET  /health          — Liveness + Redis connectivity probe.

The application is intentionally thin: all Redis logic lives in queue.py
so it can be exercised in isolation from HTTP concerns.
"""

from __future__ import annotations

import logging

import redis
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.dependencies import get_redis
from app.models import (
    HealthResponse,
    JobStatus,
    TaskEnqueuedResponse,
    TaskPayload,
    TaskStatusResponse,
)
from app.queue import enqueue_job, get_job

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("flowqueue.api")

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

settings = get_settings()

app = FastAPI(
    title="FlowQueue",
    description=(
        "Distributed async task processing engine. "
        "Enqueue jobs via POST /tasks and poll status via GET /tasks/{task_id}."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# Allow cross-origin requests (useful if a frontend polls the API directly).
# Tighten the allow_origins list in production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post(
    "/tasks",
    response_model=TaskEnqueuedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Enqueue a new task",
    tags=["tasks"],
)
def create_task(
    body: TaskPayload,
    r: redis.Redis = Depends(get_redis),
) -> TaskEnqueuedResponse:
    """
    Accept a task and push it onto the Redis queue.

    The response contains the `task_id` the caller should use to poll
    `GET /tasks/{task_id}` for results.  The endpoint returns **202
    Accepted** (not 201 Created) because the task has not been processed
    yet — only accepted for processing.

    **Example request**

    ```json
    {
        "task_type": "word_count",
        "payload": {"text": "the quick brown fox"}
    }
    ```
    """
    task_id = enqueue_job(r, body.task_type, body.payload)
    logger.info("Enqueued task %s (type=%s)", task_id, body.task_type)
    return TaskEnqueuedResponse(task_id=task_id, status=JobStatus.queued)


@app.get(
    "/tasks/{task_id}",
    response_model=TaskStatusResponse,
    summary="Get task status",
    tags=["tasks"],
)
def get_task_status(
    task_id: str,
    r: redis.Redis = Depends(get_redis),
) -> TaskStatusResponse:
    """
    Return the current state of a previously enqueued task.

    Possible `status` values:
    - **queued**     — waiting to be picked up by a worker
    - **processing** — a worker is currently executing it
    - **completed**  — finished successfully; `result` is populated
    - **failed**     — exhausted all retries; `error` is populated

    Returns **404** if the task_id is unknown or has expired (TTL elapsed).
    """
    job = get_job(r, task_id)

    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' not found or has expired.",
        )

    return TaskStatusResponse(
        task_id=job["task_id"],
        task_type=job["task_type"],
        status=JobStatus(job["status"]),
        attempts=job["attempts"],
        result=job.get("result"),
        error=job.get("error"),
    )


@app.get(
    "/health",
    response_model=HealthResponse,
    summary="Health check",
    tags=["ops"],
)
def health_check(r: redis.Redis = Depends(get_redis)) -> HealthResponse:
    """
    Liveness probe used by load balancers and Cloud Run health checks.

    Returns HTTP 200 if the API process is alive and Redis is reachable.
    Returns HTTP 503 if Redis is unreachable.

    Cloud Run will replace an unhealthy instance when this endpoint
    returns a non-2xx status, keeping the service self-healing.
    """
    try:
        r.ping()
        redis_status = "ok"
        http_status = status.HTTP_200_OK
    except Exception as exc:  # noqa: BLE001
        logger.error("Redis health check failed: %s", exc)
        redis_status = "error"
        http_status = status.HTTP_503_SERVICE_UNAVAILABLE

    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=http_status,
        content=HealthResponse(status="ok" if http_status == 200 else "degraded", redis=redis_status).model_dump(),
    )
