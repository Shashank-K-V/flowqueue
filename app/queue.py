"""
queue.py — Redis enqueue / dequeue / state-management logic.

Design decisions
----------------
* Jobs are stored as Redis hashes (HSET/HGETALL) keyed by task_id.
  Hashes give us cheap per-field updates (e.g. only updating 'status'
  without rewriting the whole blob) and a clear schema.

* The pending work list is a Redis list (LPUSH/BLPOP).  Only the
  task_id is pushed to the list; the full payload lives in the hash.
  This avoids duplicating data and keeps the list items tiny so
  Redis doesn't hold large payloads twice.

* TTL is set (or refreshed) on the hash every time the hash is written.
  We deliberately do NOT set a TTL on the queue list itself — that list
  only grows when jobs are enqueued and shrinks as workers consume them.

* All Redis operations on a single job are not wrapped in a MULTI/EXEC
  transaction.  The risk is a partial write if the process dies mid-way,
  but the worker handles this gracefully: if a job hash is missing it
  simply moves on.  A full transaction would require WATCH and a retry
  loop, adding complexity with little gain for this use-case.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, Optional

import redis

from app.config import get_settings
from app.models import JobStatus


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _job_key(task_id: str) -> str:
    """Return the Redis hash key for a given task_id."""
    return f"{get_settings().job_hash_prefix}{task_id}"


# ---------------------------------------------------------------------------
# Public API used by both the FastAPI layer and the worker
# ---------------------------------------------------------------------------

def enqueue_job(
    r: redis.Redis,
    task_type: str,
    payload: Dict[str, Any],
) -> str:
    """
    Persist job metadata to a Redis hash and push the task_id onto the
    pending queue list.

    Returns the newly generated task_id (UUID4 string).

    Redis writes (in order):
      1. HSET  flowqueue:job:<id>  — all fields atomically
      2. EXPIRE flowqueue:job:<id> — set TTL
      3. LPUSH flowqueue:jobs <id> — make the job visible to workers

    The LPUSH is last intentionally: if steps 1-2 fail, no worker will
    ever try to process a job whose hash doesn't exist yet.
    """
    settings = get_settings()
    task_id = str(uuid.uuid4())
    key = _job_key(task_id)

    job_data: Dict[str, str] = {
        "task_id": task_id,
        "task_type": task_type,
        "payload": json.dumps(payload),   # serialise dict → JSON string for Redis
        "status": JobStatus.queued,
        "attempts": "0",
        "result": "",    # empty string = not set; avoids None/null serialisation issues
        "error": "",
    }

    # Write all fields in one round-trip
    r.hset(key, mapping=job_data)
    # Expire the hash regardless of final state
    r.expire(key, settings.ttl_seconds)
    # Put the job on the queue — workers are waiting on this list
    r.lpush(settings.queue_key, task_id)

    return task_id


def get_job(r: redis.Redis, task_id: str) -> Optional[Dict[str, Any]]:
    """
    Retrieve all fields from a job hash.

    Returns None if the job does not exist (expired or never created).
    Otherwise returns a dict with all fields decoded to Python types:
      - payload → dict  (JSON-decoded)
      - attempts → int
      - result  → dict | None
      - error   → str | None
    """
    key = _job_key(task_id)
    raw: Dict[str, str] = r.hgetall(key)

    if not raw:
        return None

    # Deserialise JSON-encoded fields back to native Python types
    result: Optional[Dict[str, Any]] = None
    if raw.get("result"):
        result = json.loads(raw["result"])

    return {
        "task_id": raw["task_id"],
        "task_type": raw["task_type"],
        "payload": json.loads(raw["payload"]),
        "status": raw["status"],
        "attempts": int(raw.get("attempts", 0)),
        "result": result,
        "error": raw.get("error") or None,
    }


def update_job_status(
    r: redis.Redis,
    task_id: str,
    status: JobStatus,
    *,
    result: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    increment_attempts: bool = False,
) -> None:
    """
    Perform a partial update on an existing job hash.

    Only the fields we explicitly pass are overwritten; all other fields
    (task_type, payload, …) are left untouched.  After the update we
    refresh the TTL so that recently-modified jobs survive for the full
    TTL window from their last state change.

    Parameters
    ----------
    r:
        Redis client.
    task_id:
        Target job identifier.
    status:
        New JobStatus value.
    result:
        dict to store as the job result (completed jobs).
    error:
        Exception message to store (failed jobs).
    increment_attempts:
        When True, atomically increments the 'attempts' counter.
    """
    settings = get_settings()
    key = _job_key(task_id)

    updates: Dict[str, str] = {"status": status}

    if result is not None:
        updates["result"] = json.dumps(result)

    if error is not None:
        updates["error"] = error

    r.hset(key, mapping=updates)

    if increment_attempts:
        r.hincrby(key, "attempts", 1)

    # Always refresh TTL on every write so long-running jobs don't expire
    # partway through processing.
    r.expire(key, settings.ttl_seconds)


def dequeue_job(r: redis.Redis, timeout: int = 5) -> Optional[str]:
    """
    Block-wait for a job to appear on the queue list and return its task_id.

    Uses BLPOP with a timeout so the worker loop wakes up periodically
    even when the queue is empty (allows clean shutdown on SIGTERM).

    Returns None if the timeout expires with no job available.
    """
    settings = get_settings()
    result = r.blpop(settings.queue_key, timeout=timeout)

    if result is None:
        return None

    # BLPOP returns (key, value); we only need the value (task_id)
    _list_key, task_id = result
    return task_id
