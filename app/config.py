"""
config.py — Centralized configuration for FlowQueue.

All tuneable values come from environment variables so the same image
can be deployed to local Docker, Cloud Run, or Railway without code
changes. Defaults are chosen to work out-of-the-box with docker-compose.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """
    Application settings loaded from environment variables.

    Pydantic BaseSettings reads from:
      1. Actual environment variables (highest priority)
      2. A .env file in the working directory (if python-dotenv is installed)
      3. The field defaults below (lowest priority)
    """

    # ------------------------------------------------------------------
    # Redis
    # ------------------------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"
    """Full Redis connection URL. Use redis://:<password>@host:port/db
    for authenticated connections (e.g. Redis Cloud, Upstash)."""

    # ------------------------------------------------------------------
    # Queue keys
    # ------------------------------------------------------------------
    queue_key: str = "flowqueue:jobs"
    """Redis list key that workers BLPOP from. Change to namespace multiple
    queues in a single Redis instance."""

    job_hash_prefix: str = "flowqueue:job:"
    """Prefix for per-job Redis hashes. Final key: flowqueue:job:<task_id>"""

    # ------------------------------------------------------------------
    # Worker behaviour
    # ------------------------------------------------------------------
    max_retries: int = 3
    """Number of times a failing job is retried before being marked failed.
    Attempt 1 is the initial run; retries happen on attempts 2..max_retries+1."""

    blpop_timeout: int = 5
    """Seconds BLPOP waits for a job before returning None. Keeps the worker
    loop alive and responsive to SIGTERM without burning CPU."""

    # ------------------------------------------------------------------
    # State TTL
    # ------------------------------------------------------------------
    ttl_seconds: int = 3600
    """How long completed/failed job hashes survive in Redis (seconds).
    Default 1 hour. Prevents unbounded growth without a separate sweep."""

    # ------------------------------------------------------------------
    # API server
    # ------------------------------------------------------------------
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    log_level: str = "info"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings singleton.

    Using lru_cache means we parse env-vars exactly once per process,
    which is safe for both the API (multiple threads) and the worker
    (single thread). Tests can call get_settings.cache_clear() to reset.
    """
    return Settings()
