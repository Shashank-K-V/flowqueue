# FlowQueue

[![CI](https://github.com/Shashank-K-V//flowqueue/actions/workflows/ci.yml/badge.svg)](https://github.com/Shashank-K-V//flowqueue/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi)
![Redis](https://img.shields.io/badge/Redis-7-DC382D?logo=redis)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker)
![License](https://img.shields.io/badge/license-MIT-green)

**FlowQueue** is a horizontally scalable, distributed async task processing engine. Submit jobs via a REST API, have one or more stateless worker processes consume them from Redis, and poll for results — all wired together with Docker Compose and deployable to Google Cloud Run for free.

---

## Architecture

```
┌────────────────────────────────────────────────────────────────┐
│                         Client                                 │
│   POST /tasks  ──────────────────────────►  GET /tasks/{id}    │
└──────────────────────┬──────────────────────────▲──────────────┘
                       │                          │
                       ▼                          │
              ┌─────────────────┐                 │
              │   FastAPI API   │  (Dockerfile)   │
              │   (main.py)     │─────────────────┘
              └────────┬────────┘
                       │ LPUSH task_id
                       ▼
              ┌─────────────────┐
              │      Redis      │  flowqueue:jobs  (list)
              │   (job queue    │  flowqueue:job:<id>  (hash, TTL=1h)
              │   + state store)│
              └────────┬────────┘
                       │ BLPOP task_id
                       ▼
         ┌─────────────────────────────┐
         │   Worker  ×  N instances    │  (Dockerfile.worker)
         │   (processor.py)            │
         │                             │
         │  • executes task handler    │
         │  • updates job hash status  │
         │  • retries on failure       │
         └─────────────────────────────┘
```

**Data flow:**
1. Client POSTs a task → API writes job hash to Redis + LPUSH task_id
2. Worker BLPOPs task_id → reads payload hash → executes handler
3. On success: HSET status=completed, result=<json>
4. On failure (attempt < max): re-LPUSH task_id; on exhaustion: status=failed
5. Client GETs /tasks/{id} → API reads hash → returns current state

---

## Tech Stack

| Layer | Technology |
|---|---|
| API | FastAPI + Uvicorn |
| Queue & State | Redis 7 (LPUSH / BLPOP / HSET) |
| Worker | Python asyncio-free subprocess + BLPOP |
| Config | pydantic-settings (env vars) |
| Testing | pytest + fakeredis (no real Redis in CI) |
| Containers | Docker + Docker Compose |
| CI | GitHub Actions |
| Deployment | Google Cloud Run (free tier) |

---

## Local Setup

### Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (includes Compose)
- Python 3.11+ (only needed if running tests outside Docker)

### 1. Clone and configure

```bash
git clone https://github.com/Shashank-K-V//flowqueue.git
cd flowqueue
cp .env.example .env   # defaults work for local Docker
```

### 2. Start everything

```bash
docker compose up --build
```

This starts three containers:
- `flowqueue-redis` on `localhost:6379`
- `flowqueue-api` on `localhost:8000`
- `flowqueue-worker` (no exposed port)

### 3. Verify

```bash
curl http://localhost:8000/health
# {"status":"ok","redis":"ok"}
```

Open **http://localhost:8000/docs** for the interactive Swagger UI.

---

## API Reference

### POST /tasks — Enqueue a task

```bash
curl -X POST http://localhost:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{"task_type": "word_count", "payload": {"text": "the quick brown fox"}}'
```

**Response (202 Accepted)**
```json
{
  "task_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "status": "queued",
  "message": "Task enqueued successfully"
}
```

---

### GET /tasks/{task_id} — Poll for status

```bash
curl http://localhost:8000/tasks/f47ac10b-58cc-4372-a567-0e02b2c3d479
```

**Response — still queued**
```json
{
  "task_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "task_type": "word_count",
  "status": "queued",
  "attempts": 0,
  "result": null,
  "error": null
}
```

**Response — completed**
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

**Response — failed (all retries exhausted)**
```json
{
  "task_id": "...",
  "task_type": "word_count",
  "status": "failed",
  "attempts": 3,
  "result": null,
  "error": "'text' must be a string, got int"
}
```

**404** is returned if `task_id` is unknown or has expired (TTL elapsed).

---

### GET /health — Health probe

```bash
curl http://localhost:8000/health
```

```json
{"status": "ok", "redis": "ok"}
```

Returns **503** if Redis is unreachable.

---

## Job Status State Machine

```
                POST /tasks
                    │
                    ▼
                 queued
                    │
          worker picks up
                    │
                    ▼
              processing
               │       │
           success   failure
               │       │
               │       ├── attempt < max_retries ──► queued (re-queued)
               │       │
               │       └── attempts == max_retries ──► failed
               │
               ▼
           completed
```

---

## Running Tests

Tests use **fakeredis** — no Redis server needed.

```bash
# Install dependencies
pip install -r requirements.txt

# Run all tests
pytest tests/ -v

# Run with coverage report
pytest tests/ --cov=app --cov=worker --cov-report=term-missing

# Run a specific test file
pytest tests/test_worker.py -v
```

---

## Configuration

All settings are read from environment variables (or `.env`):

| Variable | Default | Description |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection URL |
| `QUEUE_KEY` | `flowqueue:jobs` | Redis list key for pending jobs |
| `JOB_HASH_PREFIX` | `flowqueue:job:` | Prefix for job state hashes |
| `MAX_RETRIES` | `3` | Max worker attempts per job |
| `BLPOP_TIMEOUT` | `5` | Seconds worker blocks on empty queue |
| `TTL_SECONDS` | `3600` | Job hash TTL (seconds) after completion |
| `API_PORT` | `8000` | Uvicorn listen port |
| `LOG_LEVEL` | `info` | Uvicorn / logging level |

---

## Horizontal Scaling

FlowQueue is designed to scale workers horizontally with zero code changes:

```bash
# Run 5 workers in parallel consuming from the same queue
docker compose up --scale worker=5
```

**Why it's safe:**
- Each `BLPOP` pops exactly one `task_id` — two workers never pick up the same job.
- Workers hold no in-process state; all state lives in Redis hashes.
- The API is also stateless: scale with `--scale api=N` behind a load balancer.
- On Cloud Run, set `--min-instances` and `--max-instances` for auto-scaling.

---

## Deployment — Google Cloud Run (Free Tier)

> Free tier: **2 million requests/month**, 360,000 GB-seconds of compute, always free.

See [DEPLOY.md](DEPLOY.md) for full step-by-step instructions.

**Quick summary:**

```bash
# 1. Build and push the API image
gcloud builds submit --tag gcr.io/PROJECT_ID/flowqueue-api

# 2. Deploy to Cloud Run
gcloud run deploy flowqueue-api \
  --image gcr.io/PROJECT_ID/flowqueue-api \
  --platform managed \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars REDIS_URL=rediss://:PASSWORD@HOST:PORT/0
```

The worker is deployed as a separate Cloud Run **Job** (or a second service with `--no-allow-unauthenticated`).

---

## Project Structure

```
flowqueue/
├── app/
│   ├── main.py          # FastAPI app and routes
│   ├── models.py        # Pydantic request/response schemas
│   ├── queue.py         # Redis enqueue/dequeue/state logic
│   ├── config.py        # Centralised settings (pydantic-settings)
│   └── dependencies.py  # FastAPI Depends() helpers
├── worker/
│   └── processor.py     # BLPOP consumer with retry logic
├── tests/
│   ├── conftest.py      # Shared fixtures (fakeredis, TestClient)
│   ├── test_api.py      # HTTP endpoint tests
│   ├── test_queue.py    # Redis queue unit tests
│   └── test_worker.py   # Worker retry / failure tests
├── .github/
│   └── workflows/
│       └── ci.yml       # GitHub Actions: pytest on every push
├── docker-compose.yml   # Wires API + worker + Redis
├── Dockerfile           # API image (multi-stage)
├── Dockerfile.worker    # Worker image (multi-stage)
├── requirements.txt
├── .env.example
└── README.md
```

---

## License

MIT
