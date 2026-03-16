# ── Dockerfile for the FlowQueue API service ──────────────────────────────────
#
# Multi-stage build:
#   builder  — installs dependencies into a venv
#   runtime  — copies venv into a slim final image (no build tools)
#
# This keeps the final image small (~150 MB vs ~800 MB without staging).

# ── Stage 1: dependency builder ───────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Install pip-tools for deterministic installs
RUN pip install --upgrade pip

# Copy only requirements first so Docker layer-caches the install step.
# The venv is placed under /build/venv so we can copy the whole directory
# cleanly into the runtime stage.
COPY requirements.txt .
RUN python -m venv /build/venv && \
    /build/venv/bin/pip install --no-cache-dir -r requirements.txt

# ── Stage 2: runtime image ────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Create a non-root user for security (Cloud Run best practice)
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser

WORKDIR /app

# Bring in the pre-built venv from the builder stage
COPY --from=builder /build/venv /app/venv

# Copy application source
COPY app/ ./app/

# Make the venv the active Python environment
ENV PATH="/app/venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Run as non-root
USER appuser

# Cloud Run injects PORT; default to 8000 for local Docker use.
ENV PORT=8000
EXPOSE $PORT

# Start uvicorn.  Cloud Run sets $PORT; the shell expansion handles both.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
