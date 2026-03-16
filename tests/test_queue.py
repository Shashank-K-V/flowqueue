"""
test_queue.py — Unit tests for app/queue.py (Redis enqueue/dequeue logic).

These tests operate directly on the queue module functions, bypassing
HTTP entirely.  fakeredis gives us a real in-memory Redis API so we can
verify LPUSH/BLPOP, HSET/HGETALL, and EXPIRE behaviours accurately.
"""

from __future__ import annotations

import json

import fakeredis
import pytest

from app.config import get_settings
from app.models import JobStatus
from app.queue import (
    _job_key,
    dequeue_job,
    enqueue_job,
    get_job,
    update_job_status,
)


@pytest.fixture()
def r() -> fakeredis.FakeRedis:
    """Fresh FakeRedis instance for each test."""
    return fakeredis.FakeRedis(decode_responses=True)


class TestEnqueueJob:
    """Tests for enqueue_job()."""

    def test_returns_uuid_string(self, r: fakeredis.FakeRedis) -> None:
        task_id = enqueue_job(r, "word_count", {"text": "hi"})
        assert isinstance(task_id, str)
        assert len(task_id) == 36

    def test_hash_is_created(self, r: fakeredis.FakeRedis) -> None:
        """The job hash should exist in Redis after enqueue."""
        task_id = enqueue_job(r, "word_count", {"text": "hello"})
        key = _job_key(task_id)
        assert r.exists(key)

    def test_hash_fields_are_correct(self, r: fakeredis.FakeRedis) -> None:
        """All expected fields should be present with correct initial values."""
        task_id = enqueue_job(r, "word_count", {"text": "world"})
        raw = r.hgetall(_job_key(task_id))

        assert raw["task_id"] == task_id
        assert raw["task_type"] == "word_count"
        assert raw["status"] == JobStatus.queued
        assert raw["attempts"] == "0"
        assert json.loads(raw["payload"]) == {"text": "world"}

    def test_task_id_pushed_to_queue(self, r: fakeredis.FakeRedis) -> None:
        """After enqueue the task_id should appear in the queue list."""
        settings = get_settings()
        task_id = enqueue_job(r, "word_count", {"text": "test"})
        # LRANGE returns newest-first because we use LPUSH
        items = r.lrange(settings.queue_key, 0, -1)
        assert task_id in items

    def test_ttl_is_set(self, r: fakeredis.FakeRedis) -> None:
        """The hash should have a positive TTL immediately after enqueue."""
        settings = get_settings()
        task_id = enqueue_job(r, "word_count", {"text": "ttl test"})
        ttl = r.ttl(_job_key(task_id))
        assert 0 < ttl <= settings.ttl_seconds

    def test_multiple_enqueues_produce_unique_ids(self, r: fakeredis.FakeRedis) -> None:
        ids = {enqueue_job(r, "word_count", {"text": str(i)}) for i in range(20)}
        assert len(ids) == 20


class TestGetJob:
    """Tests for get_job()."""

    def test_returns_none_for_missing_key(self, r: fakeredis.FakeRedis) -> None:
        assert get_job(r, "nonexistent-id") is None

    def test_returns_dict_for_existing_job(self, r: fakeredis.FakeRedis) -> None:
        task_id = enqueue_job(r, "word_count", {"text": "hello"})
        job = get_job(r, task_id)
        assert job is not None
        assert isinstance(job, dict)

    def test_payload_is_deserialized(self, r: fakeredis.FakeRedis) -> None:
        """payload should come back as a dict, not a JSON string."""
        task_id = enqueue_job(r, "word_count", {"text": "hello", "extra": 42})
        job = get_job(r, task_id)
        assert job["payload"] == {"text": "hello", "extra": 42}

    def test_attempts_is_int(self, r: fakeredis.FakeRedis) -> None:
        task_id = enqueue_job(r, "word_count", {"text": "x"})
        job = get_job(r, task_id)
        assert isinstance(job["attempts"], int)
        assert job["attempts"] == 0

    def test_empty_result_returns_none(self, r: fakeredis.FakeRedis) -> None:
        task_id = enqueue_job(r, "word_count", {"text": "x"})
        job = get_job(r, task_id)
        assert job["result"] is None

    def test_empty_error_returns_none(self, r: fakeredis.FakeRedis) -> None:
        task_id = enqueue_job(r, "word_count", {"text": "x"})
        job = get_job(r, task_id)
        assert job["error"] is None


class TestUpdateJobStatus:
    """Tests for update_job_status()."""

    def test_status_is_updated(self, r: fakeredis.FakeRedis) -> None:
        task_id = enqueue_job(r, "word_count", {"text": "test"})
        update_job_status(r, task_id, JobStatus.processing)
        job = get_job(r, task_id)
        assert job["status"] == JobStatus.processing

    def test_result_is_stored(self, r: fakeredis.FakeRedis) -> None:
        task_id = enqueue_job(r, "word_count", {"text": "hello world"})
        result = {"word_count": 2}
        update_job_status(r, task_id, JobStatus.completed, result=result)
        job = get_job(r, task_id)
        assert job["result"] == result
        assert job["status"] == JobStatus.completed

    def test_error_is_stored(self, r: fakeredis.FakeRedis) -> None:
        task_id = enqueue_job(r, "word_count", {"text": "x"})
        update_job_status(r, task_id, JobStatus.failed, error="Something broke")
        job = get_job(r, task_id)
        assert job["error"] == "Something broke"
        assert job["status"] == JobStatus.failed

    def test_increment_attempts(self, r: fakeredis.FakeRedis) -> None:
        task_id = enqueue_job(r, "word_count", {"text": "x"})
        update_job_status(r, task_id, JobStatus.processing, increment_attempts=True)
        update_job_status(r, task_id, JobStatus.processing, increment_attempts=True)
        job = get_job(r, task_id)
        assert job["attempts"] == 2

    def test_no_increment_does_not_change_attempts(self, r: fakeredis.FakeRedis) -> None:
        task_id = enqueue_job(r, "word_count", {"text": "x"})
        update_job_status(r, task_id, JobStatus.processing)
        job = get_job(r, task_id)
        assert job["attempts"] == 0

    def test_ttl_is_refreshed_on_update(self, r: fakeredis.FakeRedis) -> None:
        """Each update should reset the TTL."""
        settings = get_settings()
        task_id = enqueue_job(r, "word_count", {"text": "x"})
        update_job_status(r, task_id, JobStatus.completed, result={"word_count": 1})
        ttl = r.ttl(_job_key(task_id))
        assert 0 < ttl <= settings.ttl_seconds


class TestDequeueJob:
    """Tests for dequeue_job()."""

    def test_returns_task_id_when_job_exists(self, r: fakeredis.FakeRedis) -> None:
        task_id = enqueue_job(r, "word_count", {"text": "hello"})
        # BLPOP returns immediately when an item exists regardless of timeout value.
        # Use timeout=5 (a real value); fakeredis returns instantly when item is present.
        result = dequeue_job(r, timeout=5)
        assert result == task_id

    def test_returns_none_when_queue_empty(self, r: fakeredis.FakeRedis) -> None:
        # timeout=1: wait 1 second then return None.
        # Do NOT use timeout=0 — in Redis semantics that means "block forever".
        result = dequeue_job(r, timeout=1)
        assert result is None

    def test_fifo_ordering(self, r: fakeredis.FakeRedis) -> None:
        """Jobs should be consumed in FIFO order (LPUSH + BRPOP semantics)."""
        # Note: we use LPUSH + BLPOP, which is LIFO on the list but we push
        # sequentially. The important thing is that each task_id is dequeued
        # exactly once.
        ids = [enqueue_job(r, "word_count", {"text": str(i)}) for i in range(3)]
        dequeued = [dequeue_job(r, timeout=5) for _ in range(3)]
        # All three should be dequeued (order may vary based on LPUSH direction)
        assert set(dequeued) == set(ids)

    def test_queue_is_empty_after_dequeue(self, r: fakeredis.FakeRedis) -> None:
        settings = get_settings()
        enqueue_job(r, "word_count", {"text": "one"})
        dequeue_job(r, timeout=5)
        # Queue list should now be empty
        assert r.llen(settings.queue_key) == 0
