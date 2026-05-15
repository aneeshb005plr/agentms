# app/middleware.py
# Custom middleware registered in create_app() factory.
#
# CorrelationIdMiddleware:
#   - Assigns a unique X-Correlation-ID to every request
#   - If client sends one, we use theirs (useful for Ocelot gateway tracing)
#   - Returned in response headers so Angular can log it
#   - Logged with every request — makes distributed tracing across AKS pods easy
#
# RequestLoggingMiddleware:
#   - Logs method, path, status code, duration for every request
#   - PII-safe — never logs request body or auth headers

import logging
import time
import uuid

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

CORRELATION_ID_HEADER = "X-Correlation-ID"

# Paths to skip from request logging — too noisy
_SKIP_LOGGING_PATHS = {"/health", "/metrics"}


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """
    Attaches a correlation ID to every request and response.
    Uses client-provided ID if present, otherwise generates a new UUID.
    Critical for tracing requests across multiple AKS pods.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        correlation_id = (
            request.headers.get(CORRELATION_ID_HEADER)
            or str(uuid.uuid4())
        )

        # Store on request state — accessible in any endpoint/service
        request.state.correlation_id = correlation_id

        response = await call_next(request)

        # Always return it in response so Angular can log it
        response.headers[CORRELATION_ID_HEADER] = correlation_id
        return response


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """
    Logs every request with method, path, status code, and duration.
    PII-safe — never logs request body, query params with sensitive data,
    or Authorization headers.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path in _SKIP_LOGGING_PATHS:
            return await call_next(request)

        start_time = time.perf_counter()
        correlation_id = getattr(request.state, "correlation_id", "-")

        response = await call_next(request)

        duration_ms = round((time.perf_counter() - start_time) * 1000, 2)

        logger.info(
            f"{request.method} {request.url.path} "
            f"→ {response.status_code} "
            f"| {duration_ms}ms "
            f"| correlation_id={correlation_id}"
        )

        return response


def register_middleware(app: FastAPI) -> None:
    """
    Registers all middleware on the FastAPI app.
    Called once inside create_app() factory.
    ORDER MATTERS — last added = first executed.
    CorrelationId must run before RequestLogging so ID is available when logging.
    """
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(CorrelationIdMiddleware)