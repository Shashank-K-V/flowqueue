"""
models.py — Pydantic request/response schemas for FlowQueue.

Keeping schemas in their own module makes them importable by both the
API layer (main.py) and the worker (processor.py) without circular
imports.  It also gives us a single source of truth for the wire
format.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Job state machine
# ---------------------------------------------------------------------------

class JobStatus(str, Enum):
    """
    All valid states a job can be in.

    State transitions:
        (enqueue)           (worker picks up)       (success)
        queued  ──────────► processing  ──────────► completed
                                │
                                │  (exception, attempt < max_retries)
                                ▼
                            re-queued  ──► processing  ──► ...
                                │
                                │  (exception, attempts exhausted)
                                ▼
                             failed

    Using `str` as the mixin means JobStatus.queued == "queued", which
    lets us store/compare values without extra serialisation steps.
    """

    queued = "queued"
    processing = "processing"
    completed = "completed"
    failed = "failed"


# ---------------------------------------------------------------------------
# API request schemas
# ---------------------------------------------------------------------------

class TaskPayload(BaseModel):
    """
    The body of POST /tasks.

    Example::

        {
            "task_type": "word_count",
            "payload": {"text": "hello world foo"}
        }
    """

    task_type: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Identifier for the kind of work the worker should do.",
        examples=["word_count"],
    )
    payload: Dict[str, Any] = Field(
        ...,
        description="Arbitrary JSON data forwarded to the worker unchanged.",
        examples=[{"text": "The quick brown fox"}],
    )


# ---------------------------------------------------------------------------
# API response schemas
# ---------------------------------------------------------------------------

class TaskEnqueuedResponse(BaseModel):
    """
    Returned immediately after POST /tasks succeeds.

    The caller should poll GET /tasks/{task_id} until status is
    'completed' or 'failed'.
    """

    task_id: str = Field(..., description="UUID assigned to this job.")
    status: JobStatus = Field(
        JobStatus.queued,
        description="Always 'queued' at enqueue time.",
    )
    message: str = Field(
        "Task enqueued successfully",
        description="Human-readable confirmation.",
    )


class TaskStatusResponse(BaseModel):
    """
    Returned by GET /tasks/{task_id}.

    `result` is populated only when status == 'completed'.
    `error`  is populated only when status == 'failed'.
    `attempts` tracks how many times the worker has tried this job.
    """

    task_id: str
    task_type: str
    status: JobStatus
    attempts: int = Field(0, ge=0, description="Number of execution attempts made.")
    result: Optional[Dict[str, Any]] = Field(
        None,
        description="Worker output (populated when status == 'completed').",
    )
    error: Optional[str] = Field(
        None,
        description="Last exception message (populated when status == 'failed').",
    )


class HealthResponse(BaseModel):
    """Returned by GET /health."""

    status: str = "ok"
    redis: str = Field(..., description="'ok' if Redis is reachable, else 'error'.")
