"""
conftest.py — Shared pytest fixtures for FlowQueue tests.

fakeredis is a pure-Python drop-in replacement for redis-py that runs
entirely in-process with no real Redis server required.  This makes our
CI pipeline dependency-free and our test suite fast.

Key fixtures
------------
fake_redis   : a FakeRedis instance reset for each test function.
test_app     : a FastAPI TestClient with get_redis overridden to use
               fake_redis, so HTTP tests never touch a real server.
"""

from __future__ import annotations

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.dependencies import get_redis
from app.main import app


@pytest.fixture()
def fake_redis() -> fakeredis.FakeRedis:
    """
    Return a fresh FakeRedis instance for each test.

    FakeRedis(decode_responses=True) mirrors the decode_responses=True
    setting used in production so tests exercise the same code paths.
    """
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture()
def test_app(fake_redis: fakeredis.FakeRedis) -> TestClient:
    """
    Return a FastAPI TestClient backed by fakeredis.

    We override the get_redis dependency so every route that calls
    Depends(get_redis) receives our fake_redis instead of opening a
    real TCP connection.  The override is cleared after the test so it
    does not bleed into other tests.
    """
    app.dependency_overrides[get_redis] = lambda: fake_redis
    client = TestClient(app, raise_server_exceptions=True)
    yield client
    app.dependency_overrides.clear()


@pytest.fixture()
def settings():
    """Expose the settings singleton for tests that need config values."""
    return get_settings()
