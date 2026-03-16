"""
test_api.py — Integration tests for the FastAPI HTTP layer.

Tests exercise the full request/response cycle through FastAPI's
TestClient (which calls route handlers synchronously).  All Redis
operations hit fakeredis — no real server needed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.models import JobStatus


class TestPostTasks:
    """Tests for POST /tasks."""

    def test_enqueue_returns_202(self, test_app: TestClient) -> None:
        """A valid enqueue request should return HTTP 202 Accepted."""
        response = test_app.post(
            "/tasks",
            json={"task_type": "word_count", "payload": {"text": "hello world"}},
        )
        assert response.status_code == 202

    def test_enqueue_response_has_task_id(self, test_app: TestClient) -> None:
        """The response body must contain a task_id string."""
        response = test_app.post(
            "/tasks",
            json={"task_type": "word_count", "payload": {"text": "hello"}},
        )
        body = response.json()
        assert "task_id" in body
        assert isinstance(body["task_id"], str)
        assert len(body["task_id"]) == 36  # UUID4 length including hyphens

    def test_enqueue_response_status_is_queued(self, test_app: TestClient) -> None:
        """Freshly enqueued jobs must report status 'queued'."""
        response = test_app.post(
            "/tasks",
            json={"task_type": "word_count", "payload": {"text": "test"}},
        )
        assert response.json()["status"] == JobStatus.queued

    def test_enqueue_missing_task_type_returns_422(self, test_app: TestClient) -> None:
        """Missing required field task_type should return 422 Unprocessable Entity."""
        response = test_app.post(
            "/tasks",
            json={"payload": {"text": "oops"}},
        )
        assert response.status_code == 422

    def test_enqueue_missing_payload_returns_422(self, test_app: TestClient) -> None:
        """Missing required field payload should return 422."""
        response = test_app.post(
            "/tasks",
            json={"task_type": "word_count"},
        )
        assert response.status_code == 422

    def test_enqueue_empty_task_type_returns_422(self, test_app: TestClient) -> None:
        """Empty string for task_type violates min_length=1 constraint."""
        response = test_app.post(
            "/tasks",
            json={"task_type": "", "payload": {}},
        )
        assert response.status_code == 422

    def test_enqueue_two_tasks_get_different_ids(self, test_app: TestClient) -> None:
        """Each enqueue call must produce a unique task_id."""
        r1 = test_app.post(
            "/tasks",
            json={"task_type": "word_count", "payload": {"text": "a"}},
        )
        r2 = test_app.post(
            "/tasks",
            json={"task_type": "word_count", "payload": {"text": "b"}},
        )
        assert r1.json()["task_id"] != r2.json()["task_id"]


class TestGetTaskStatus:
    """Tests for GET /tasks/{task_id}."""

    def test_get_queued_task(self, test_app: TestClient) -> None:
        """A task just enqueued should be retrievable and show status 'queued'."""
        post_resp = test_app.post(
            "/tasks",
            json={"task_type": "word_count", "payload": {"text": "hello"}},
        )
        task_id = post_resp.json()["task_id"]

        get_resp = test_app.get(f"/tasks/{task_id}")
        assert get_resp.status_code == 200

        body = get_resp.json()
        assert body["task_id"] == task_id
        assert body["status"] == JobStatus.queued
        assert body["task_type"] == "word_count"
        assert body["result"] is None
        assert body["error"] is None

    def test_get_unknown_task_returns_404(self, test_app: TestClient) -> None:
        """Requesting a non-existent task_id must return 404."""
        response = test_app.get("/tasks/00000000-0000-0000-0000-000000000000")
        assert response.status_code == 404

    def test_get_task_attempts_starts_at_zero(self, test_app: TestClient) -> None:
        """Freshly enqueued job should have 0 attempts."""
        post_resp = test_app.post(
            "/tasks",
            json={"task_type": "word_count", "payload": {"text": "x"}},
        )
        task_id = post_resp.json()["task_id"]
        get_resp = test_app.get(f"/tasks/{task_id}")
        assert get_resp.json()["attempts"] == 0

    def test_full_enqueue_and_status_flow(self, test_app: TestClient) -> None:
        """End-to-end: enqueue → status check → verify all fields present."""
        payload = {"text": "the quick brown fox jumps over the lazy dog"}
        post_resp = test_app.post(
            "/tasks",
            json={"task_type": "word_count", "payload": payload},
        )
        assert post_resp.status_code == 202
        task_id = post_resp.json()["task_id"]

        status_resp = test_app.get(f"/tasks/{task_id}")
        assert status_resp.status_code == 200
        body = status_resp.json()

        # All expected fields must be present
        for field in ("task_id", "task_type", "status", "attempts", "result", "error"):
            assert field in body, f"Missing field: {field}"


class TestHealthEndpoint:
    """Tests for GET /health."""

    def test_health_returns_200_with_good_redis(self, test_app: TestClient) -> None:
        """Health endpoint should return 200 when Redis is reachable."""
        response = test_app.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["redis"] == "ok"

    def test_health_returns_redis_ok_field(self, test_app: TestClient) -> None:
        """Health response body must include a 'redis' field."""
        response = test_app.get("/health")
        assert "redis" in response.json()
