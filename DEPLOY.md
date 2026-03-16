# Deploying FlowQueue to Google Cloud Run (Free Tier)

Google Cloud Run free tier gives you **2 million requests/month** and
**360,000 GB-seconds of compute per month** — more than enough to run
FlowQueue at moderate traffic with zero cost.

---

## Prerequisites

| Tool | Install |
|---|---|
| Google Cloud CLI | https://cloud.google.com/sdk/docs/install |
| Docker Desktop | https://www.docker.com/products/docker-desktop |
| A GCP project | https://console.cloud.google.com/projectcreate |
| Upstash Redis (free) | https://upstash.com (free tier: 10k req/day) |

---

## Architecture on Cloud Run

```
Internet
    │
    ▼
Cloud Run (API service)   ←→   Upstash Redis (managed Redis)
    │                               ▲
    │                               │
Cloud Run Job (Worker)  ────────────┘
```

- **API** = a Cloud Run *service* (HTTP, auto-scales to 0)
- **Worker** = a Cloud Run *job* running continuously (or a second service)
- **Redis** = Upstash free tier (no infra to manage)

---

## Step 1 — Set up Upstash Redis (free, no credit card)

1. Go to https://upstash.com → **Create Database**
2. Choose **Redis**, region **us-east-1**, **Free** tier
3. Copy the **Redis URL** — it looks like:
   ```
   rediss://default:PASSWORD@HOSTNAME:PORT
   ```
   Save this — you'll use it as `REDIS_URL` in all services.

---

## Step 2 — Authenticate with Google Cloud

```bash
# Install and initialise the CLI
gcloud auth login
gcloud config set project YOUR_PROJECT_ID

# Enable required APIs
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com
```

---

## Step 3 — Create an Artifact Registry repository

```bash
gcloud artifacts repositories create flowqueue \
  --repository-format=docker \
  --location=us-central1 \
  --description="FlowQueue container images"

# Authenticate Docker to push to Artifact Registry
gcloud auth configure-docker us-central1-docker.pkg.dev
```

---

## Step 4 — Build and push the API image

```bash
# From the project root
docker build -t us-central1-docker.pkg.dev/YOUR_PROJECT_ID/flowqueue/api:latest .
docker push us-central1-docker.pkg.dev/YOUR_PROJECT_ID/flowqueue/api:latest
```

Or use Cloud Build (builds in the cloud — no local Docker required):

```bash
gcloud builds submit \
  --tag us-central1-docker.pkg.dev/YOUR_PROJECT_ID/flowqueue/api:latest \
  --dockerfile Dockerfile \
  .
```

---

## Step 5 — Deploy the API service

```bash
gcloud run deploy flowqueue-api \
  --image us-central1-docker.pkg.dev/YOUR_PROJECT_ID/flowqueue/api:latest \
  --platform managed \
  --region us-central1 \
  --allow-unauthenticated \
  --min-instances 0 \
  --max-instances 10 \
  --memory 512Mi \
  --cpu 1 \
  --set-env-vars "REDIS_URL=rediss://default:PASSWORD@HOSTNAME:PORT,MAX_RETRIES=3,TTL_SECONDS=3600"
```

After deploy, Cloud Run prints the service URL:
```
Service URL: https://flowqueue-api-xxxx-uc.a.run.app
```

Test it:
```bash
curl https://flowqueue-api-xxxx-uc.a.run.app/health
# {"status":"ok","redis":"ok"}
```

---

## Step 6 — Build and push the Worker image

```bash
docker build -f Dockerfile.worker \
  -t us-central1-docker.pkg.dev/YOUR_PROJECT_ID/flowqueue/worker:latest .
docker push us-central1-docker.pkg.dev/YOUR_PROJECT_ID/flowqueue/worker:latest
```

---

## Step 7 — Deploy the Worker as a Cloud Run Service

The worker runs a continuous loop (BLPOP), so deploy it as a **service**
with `--min-instances=1` (keeps one instance always warm):

```bash
gcloud run deploy flowqueue-worker \
  --image us-central1-docker.pkg.dev/YOUR_PROJECT_ID/flowqueue/worker:latest \
  --platform managed \
  --region us-central1 \
  --no-allow-unauthenticated \
  --min-instances 1 \
  --max-instances 5 \
  --memory 256Mi \
  --cpu 1 \
  --set-env-vars "REDIS_URL=rediss://default:PASSWORD@HOSTNAME:PORT,MAX_RETRIES=3,TTL_SECONDS=3600,BLPOP_TIMEOUT=5" \
  --no-traffic    # worker doesn't need HTTP traffic; it self-starts the loop
```

> **Note:** Cloud Run services need to listen on a port. The worker doesn't
> have an HTTP server, so add a tiny health HTTP listener OR deploy as a
> Cloud Run **Job** for batch processing. For a long-running daemon, the
> service approach with `--min-instances=1` works well.

**Alternative: run worker as a Cloud Run Job (one-shot)**

```bash
gcloud run jobs create flowqueue-worker-job \
  --image us-central1-docker.pkg.dev/YOUR_PROJECT_ID/flowqueue/worker:latest \
  --region us-central1 \
  --set-env-vars "REDIS_URL=rediss://default:PASSWORD@HOSTNAME:PORT,MAX_RETRIES=3"

# Execute the job
gcloud run jobs execute flowqueue-worker-job --region us-central1
```

---

## Step 8 — Verify the full flow

```bash
# 1. Enqueue a task
curl -X POST https://flowqueue-api-xxxx-uc.a.run.app/tasks \
  -H "Content-Type: application/json" \
  -d '{"task_type": "word_count", "payload": {"text": "hello cloud run world"}}'

# Save the task_id from the response, then:

# 2. Poll for result
curl https://flowqueue-api-xxxx-uc.a.run.app/tasks/TASK_ID_HERE
```

---

## Updating a deployment

```bash
# Rebuild and push
docker build -t us-central1-docker.pkg.dev/YOUR_PROJECT_ID/flowqueue/api:latest .
docker push us-central1-docker.pkg.dev/YOUR_PROJECT_ID/flowqueue/api:latest

# Deploy new revision (zero-downtime rolling update)
gcloud run deploy flowqueue-api \
  --image us-central1-docker.pkg.dev/YOUR_PROJECT_ID/flowqueue/api:latest \
  --region us-central1
```

---

## Cost estimate (free tier)

| Resource | Free Tier Limit | Typical FlowQueue usage |
|---|---|---|
| Cloud Run requests | 2M/month | ~100K/month for moderate load |
| Cloud Run CPU (GB-s) | 360,000/month | ~50,000/month with min=0 |
| Cloud Run RAM (GB-s) | 180,000/month | ~20,000/month |
| Artifact Registry | 0.5 GB free | ~200 MB for both images |
| Upstash Redis | 10K req/day free | sufficient for dev/staging |

**Result: $0/month for development and low-traffic production.**

---

## Backup: Railway.app

If you prefer Railway (simpler, GUI-based):

1. Go to https://railway.app → New Project → Deploy from GitHub
2. Railway auto-detects `docker-compose.yml` and deploys all three services
3. Add environment variable `REDIS_URL` pointing to Railway's built-in Redis plugin
4. Free tier gives **$5 credit/month** (covers ~500 hours of a 512 MB container)

Railway is the fastest path from code to URL — takes ~3 minutes.
