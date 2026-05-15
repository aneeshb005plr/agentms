# Dockerfile
# ─────────────────────────────────────────────────────────────────────────────
# NextGenAMS Agent Engine
# Multi-stage build — keeps final image lean for AKS deployment
# ─────────────────────────────────────────────────────────────────────────────

# ── Stage 1: Builder ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# Install dependencies first (Docker layer cache — only re-runs if requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ── Stage 2: Runtime ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Non-root user for security — PwC/AKS requirement
RUN groupadd -r appuser && useradd -r -g appuser appuser

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY . .

# Set ownership
RUN chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# ── Environment ───────────────────────────────────────────────────────────────
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ENVIRONMENT=production

# ── Port ──────────────────────────────────────────────────────────────────────
# Ocelot gateway routes to this port inside AKS
EXPOSE 8000

# ── Health check ──────────────────────────────────────────────────────────────
# AKS liveness probe hits /health every 30 seconds
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/health').raise_for_status()"

# ── Start ─────────────────────────────────────────────────────────────────────
# workers=1 — LangGraph keeps in-memory task registry (running_tasks dict)
# Multiple workers would break stop/cancel functionality
# Scale via AKS pods (horizontal) NOT uvicorn workers (vertical)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]