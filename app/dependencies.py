"""
dependencies.py — FastAPI dependency-injection helpers.

FastAPI's Depends() system calls these functions once per request and
injects the result into the route handler. We use it to provide a
per-request Redis client that is automatically returned to the
connection pool after the response is sent.
"""

from typing import Generator

import redis

from app.config import get_settings


def get_redis() -> Generator[redis.Redis, None, None]:
    """
    Yield a Redis client for the duration of a single HTTP request.

    The client is drawn from redis-py's built-in connection pool
    (created lazily on the first call) and released back to the pool
    when the generator's finally block runs.  This means:

    - No connection is held open while the event loop is idle.
    - The pool is shared across all requests in the same process.
    - Tests can override this dependency with fakeredis via
      app.dependency_overrides[get_redis].

    Usage in a route::

        @router.get("/example")
        def example(r: redis.Redis = Depends(get_redis)):
            return r.ping()
    """
    settings = get_settings()
    client: redis.Redis = redis.from_url(
        settings.redis_url,
        decode_responses=True,   # always return str, not bytes
        socket_connect_timeout=5,
        socket_timeout=5,
    )
    try:
        yield client
    finally:
        client.close()
