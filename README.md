# FlowQueue ⚡

[![CI](https://github.com/Shashank-K-V/flowqueue/actions/workflows/ci.yml/badge.svg)](https://github.com/Shashank-K-V/flowqueue/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)
![Redis](https://img.shields.io/badge/Redis-7-DC382D?logo=redis&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)
![GCP](https://img.shields.io/badge/GCP-Cloud%20Run-4285F4?logo=googlecloud&logoColor=white)

A distributed asynchronous task queue engine built with FastAPI and Redis. Decouples task submission from execution — clients enqueue jobs via REST, stateless workers consume them from Redis using `BLPOP`, and results are tracked in TTL-managed Redis hashes. Containerized with Docker Compose for local development and deployable to GCP Cloud Run with a single command.

---

## Key Features

- **Async task submission** via a clean REST API (202 Accepted pattern)
- **Redis-backed queue** using `LPUSH`/`BLPOP` for reliable job delivery
- **Concurrent worker pool** — run `N` stateless workers with `--scale worker=N`
- **Automatic retry logic** — configurable max attempts before marking a job `failed`
- **TTL-managed state** — completed job hashes expire automatically (default: 1 hour)
- **Status polling endpoint** — track `queued → processing → completed/failed` lifecycle
- **Graceful shutdown** — workers finish in-flight jobs before exiting on `SIGTERM`
- **Zero-dependency testing** — full pytest suite using `fakeredis` (no real Redis in CI)
- **One-command local setup** via Docker Compose

---

## Tech Stack

| Layer | Technology |
|---|---|
| API Server | Python · FastAPI · Uvicorn |
| Queue & State Store | Redis 7 (`LPUSH` / `BLPOP` / `HSET` / `EXPIRE`) |
| Worker Runtime | Python subprocess · BLPOP event loop |
| Configuration | `pydantic-settings` (12-factor env vars) |
| Testing | `pytest` · `fakeredis` · `httpx` |
| Containerization | Docker · Docker Compose |
| CI | GitHub Actions |
| Deployment | GCP Cloud Run (free tier) |

---

## Architecture

```
                        ┌─────────────────────────────────────────────┐
                        │                   CLIENT                     │
                        │  POST /tasks              GET /tasks/{id}    │
                        └──────────┬────────────────────────▲──────────┘
                                   │                        │
                                   ▼                        │
                        ┌─────────────────────┐             │
                        │     FastAPI API      │─────────────┘
                        │     (main.py)        │   reads job hash
                        └──────────┬──────────┘
                                   │ LPUSH task_id
                                   ▼
                        ┌─────────────────────┐
                        │        Redis         │
                        │  flowqueue:jobs      │  ← pending queue (list)
                        │  flowqueue:job:<id>  │  ← job state (hash, TTL=1h)
                        └──────────┬──────────┘
                                   │ BLPOP task_id
                                   ▼
                  ┌────────────────────────────────────┐
                  │         Worker Pool  (×N)           │
                  │         (processor.py)              │
                  │                                     │
                  │  1. dequeue task_id via BLPOP        │
                  │  2. mark status = processing         │
                  │  3. execute task handler             │
                  │  4a. success → status = completed    │
                  │  4b. failure → retry or → failed     │
                  └────────────────────────────────────┘
```

**Request lifecycle:**

```
Client → POST /tasks → [Redis Queue] → Worker → [Redis Hash] → Client polls GET /tasks/{id}
```

---

## Local Setup

### Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (includes Compose v2)

### Run

```bash
git clone https://github.com/Shashank-K-V/flowqueue.git
cd flowqueue
cp .env.example .env
docker compose up --build
```

This starts three containers:

| Container | Role | Port |
|---|---|---|
| `flowqueue-redis` | Job queue + state store | `6379` |
| `flowqueue-api` | FastAPI REST server | `8000` |
| `flowqueue-worker` | BLPOP consumer | — |

Verify the stack is healthy:

```bash
curl http://localhost:8000/health
# {"status":"ok","redis":"ok"}
```

Swagger UI: **http://localhost:8000/docs**

---

## API Reference

### `POST /tasks` — Submit a task

```bash
curl -X POST http://localhost:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{"task_type": "word_count", "payload": {"text": "the quick brown fox"}}'
```

```json
{
  "task_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "status": "queued",
  "message": "Task enqueued successfully"
}
```

Returns **202 Accepted**. The task has been accepted for processing, not yet executed.

---

### `GET /tasks/{task_id}` — Poll task status

```bash
curl http://localhost:8000/tasks/f47ac10b-58cc-4372-a567-0e02b2c3d479
```

**Processing:**
```json
{"task_id": "f47ac10b...", "task_type": "word_count", "status": "processing", "attempts": 1, "result": null, "error": null}
```

**Completed:**
```json
{
  "task_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "task_type": "word_count",
  "status": "completed",
  "attempts": 1,
  "result": {
    "word_count": 4,
    "unique_word_count": 4,
    "char_count": 16,
    "text_preview": "the quick brown fox"
  },
  "error": null
}
```

**Failed (retries exhausted):**
```json
{"task_id": "...", "status": "failed", "attempts": 3, "result": null, "error": "'text' must be a string, got int"}
```

Returns **404** if `task_id` is unknown or has expired (TTL elapsed).

---

### `GET /health` — Liveness probe

```bash
curl http://localhost:8000/health
# 200 → {"status": "ok",       "redis": "ok"}
# 503 → {"status": "degraded", "redis": "error"}
```

Used by Cloud Run and load balancers for health-gating.

---

## Job State Machine

```
POST /tasks
     │
     ▼
  queued ──────────────── worker picks up
                                │
                                ▼
                           processing
                            │       │
                        success   exception
                            │       │
                            │       ├── attempt < max_retries ──► re-queued
                            │       └── attempts == max_retries ──► failed
                            ▼
                        completed
```

---

## Running Tests

Tests use `fakeredis` — no Redis server or Docker required.

```bash
pip install -r requirements.txt

# Full suite
pytest tests/ -v

# With coverage
pytest tests/ --cov=app --cov=worker --cov-report=term-missing

# Single module
pytest tests/test_worker.py -v
```

Test coverage includes: API endpoint flows, queue enqueue/dequeue behavior, worker retry logic, failure exhaustion, and TTL refresh.

---

## Configuration

All values are read from environment variables or `.env`:

| Variable | Default | Description |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection URL |
| `QUEUE_KEY` | `flowqueue:jobs` | Redis list key for the job queue |
| `JOB_HASH_PREFIX` | `flowqueue:job:` | Key prefix for job state hashes |
| `MAX_RETRIES` | `3` | Max worker attempts before marking failed |
| `BLPOP_TIMEOUT` | `5` | Seconds to block on an empty queue |
| `TTL_SECONDS` | `3600` | Job hash TTL after last state change |
| `API_PORT` | `8000` | Uvicorn listen port |
| `LOG_LEVEL` | `info` | Log verbosity |

---

## Scalability

Workers are **stateless** — they hold no in-process state between jobs. All shared state lives exclusively in Redis hashes keyed by `task_id`.

```bash
# Scale to 5 parallel workers consuming from the same queue
docker compose up --scale worker=5
```

**Why this is safe:**
- `BLPOP` is atomic — two workers never dequeue the same `task_id` from the same enqueue event.
- Redis is the single source of truth; worker processes are interchangeable.
- The API is also stateless and can be scaled behind any load balancer.
- On Cloud Run, set `--min-instances` / `--max-instances` for demand-driven auto-scaling.

---

## Deployment — GCP Cloud Run

> Free tier: **2 million requests/month** · 360,000 vCPU-seconds · 180,000 GB-seconds.

See [DEPLOY.md](DEPLOY.md) for the full step-by-step walkthrough.

```bash
# Build and push API image to Container Registry
gcloud builds submit --tag gcr.io/$PROJECT_ID/flowqueue-api

# Deploy API to Cloud Run
gcloud run deploy flowqueue-api \
  --image gcr.io/$PROJECT_ID/flowqueue-api \
  --platform managed \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars REDIS_URL=rediss://:PASSWORD@HOST:PORT/0

# Deploy worker as a separate Cloud Run service
gcloud run deploy flowqueue-worker \
  --image gcr.io/$PROJECT_ID/flowqueue-worker \
  --platform managed \
  --region us-central1 \
  --no-allow-unauthenticated \
  --set-env-vars REDIS_URL=rediss://:PASSWORD@HOST:PORT/0
```

Use [Upstash Redis](https://upstash.com/) (free tier) for the managed Redis instance on Cloud Run.

---

## Project Structure

```
flowqueue/
├── app/
│   ├── main.py           # FastAPI app, routes
│   ├── models.py         # Pydantic request/response schemas
│   ├── queue.py          # Redis enqueue/dequeue/state logic
│   ├── config.py         # Centralised settings (pydantic-settings)
│   └── dependencies.py   # FastAPI Depends() helpers
├── worker/
│   └── processor.py      # BLPOP consumer, retry logic, task handlers
├── tests/
│   ├── conftest.py       # Shared fixtures (fakeredis, TestClient)
│   ├── test_api.py       # HTTP endpoint tests
│   ├── test_queue.py     # Queue unit tests
│   └── test_worker.py    # Worker retry and failure tests
├── .github/
│   └── workflows/
│       └── ci.yml        # GitHub Actions — pytest on every push
├── docker-compose.yml    # Local orchestration (API + worker + Redis)
├── Dockerfile            # API image
├── Dockerfile.worker     # Worker image
├── requirements.txt
├── .env.example
├── DEPLOY.md             # Full GCP Cloud Run deployment guide
└── README.md
```

---

## License

MIT
