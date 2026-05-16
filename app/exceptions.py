# app/exceptions.py
# Custom exception classes and FastAPI exception handlers.

import logging
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

logger = logging.getLogger(__name__)


# ── Custom Exception Classes ──────────────────────────────────────────────────

class NextGenAMSException(Exception):
    def __init__(self, message: str, status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR):
        self.message     = message
        self.status_code = status_code
        super().__init__(message)

class NotFoundError(NextGenAMSException):
    def __init__(self, resource: str, identifier: str):
        super().__init__(f"{resource} not found: {identifier}", status.HTTP_404_NOT_FOUND)

class UnauthorizedError(NextGenAMSException):
    def __init__(self, detail: str = "Authentication required"):
        super().__init__(detail, status.HTTP_401_UNAUTHORIZED)

class ForbiddenError(NextGenAMSException):
    def __init__(self, detail: str = "Insufficient permissions"):
        super().__init__(detail, status.HTTP_403_FORBIDDEN)

class ValidationError(NextGenAMSException):
    def __init__(self, detail: str):
        super().__init__(detail, status.HTTP_422_UNPROCESSABLE_ENTITY)

class ExternalServiceError(NextGenAMSException):
    def __init__(self, service: str, detail: str):
        super().__init__(f"{service} error: {detail}", status.HTTP_502_BAD_GATEWAY)

class AgentError(NextGenAMSException):
    def __init__(self, agent: str, detail: str):
        super().__init__(f"Agent [{agent}] failed: {detail}", status.HTTP_500_INTERNAL_SERVER_ERROR)


# ── Exception Handlers ────────────────────────────────────────────────────────

def _error_response(status_code: int, message: str, detail: str | None = None) -> JSONResponse:
    content: dict = {"error": message}
    if detail:
        content["detail"] = detail
    return JSONResponse(status_code=status_code, content=content)


def register_exception_handlers(app: FastAPI) -> None:

    @app.exception_handler(NextGenAMSException)
    async def nextgenams_handler(request: Request, exc: NextGenAMSException) -> JSONResponse:
        logger.error("[%s] %s | path=%s", exc.__class__.__name__, exc.message, request.url.path)
        return _error_response(exc.status_code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        logger.warning("Validation error | path=%s | %s", request.url.path, exc.errors())
        return _error_response(status.HTTP_422_UNPROCESSABLE_ENTITY, "Request validation failed", str(exc.errors()))

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception | path=%s | %s", request.url.path, str(exc))
        return _error_response(status.HTTP_500_INTERNAL_SERVER_ERROR, "An unexpected error occurred.")