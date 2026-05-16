# app/api/v1/health.py
# Liveness and readiness probes for AKS.

from fastapi import APIRouter
from app.config import settings
from app.db import db

router = APIRouter(tags=["Health"])


@router.get("/health")
async def health():
    """Liveness probe — returns 200 if service is running."""
    return {
        "status":  "ok",
        "service": settings.SERVICE_NAME,
        "version": settings.SERVICE_VERSION,
        "env":     settings.ENVIRONMENT,
    }


@router.get("/health/ready")
async def readiness():
    """Readiness probe — checks MongoDB and Redis are reachable."""
    checks: dict[str, str] = {}

    try:
        await db.mongo_client.admin.command("ping")
        checks["mongodb"] = "ok"
    except Exception as e:
        checks["mongodb"] = f"error: {str(e)}"

    try:
        await db.redis.ping()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"error: {str(e)}"

    all_healthy = all(v == "ok" for v in checks.values())
    return {
        "status": "ok" if all_healthy else "degraded",
        "checks": checks,
    }