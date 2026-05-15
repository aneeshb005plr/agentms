# app/api/v1/health.py
# Health check endpoints.
# AKS liveness and readiness probes hit these.
# Ocelot gateway also uses /health to determine if pod is available.

from fastapi import APIRouter
from app.db import db
from app.config import settings

router = APIRouter(tags=["Health"])


@router.get("/health")
async def health():
    """
    Basic liveness probe — returns 200 if service is running.
    AKS hits this every 30 seconds.
    """
    return {
        "status":  "ok",
        "service": settings.SERVICE_NAME,
        "version": settings.SERVICE_VERSION,
        "env":     settings.ENVIRONMENT
    }


@router.get("/health/ready")
async def readiness():
    """
    Readiness probe — checks MongoDB and Redis are reachable.
    AKS uses this to decide if pod should receive traffic.
    Returns 200 only when all dependencies are healthy.
    """
    checks: dict[str, str] = {}

    # Check MongoDB
    try:
        await db.mongo_client.admin.command("ping")
        checks["mongodb"] = "ok"
    except Exception as e:
        checks["mongodb"] = f"error: {str(e)}"

    # Check Redis
    try:
        await db.redis.ping()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"error: {str(e)}"

    all_healthy = all(v == "ok" for v in checks.values())

    return {
        "status": "ok" if all_healthy else "degraded",
        "checks": checks
    }