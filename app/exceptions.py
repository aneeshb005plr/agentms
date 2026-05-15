# app/exceptions.py
# Custom exception classes and FastAPI exception handlers.
# All HTTP errors raised anywhere in the app use these classes.
# Registered in create_app() factory in main.py.

import logging
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

logger = logging.getLogger(__name__)


# ── Custom Exception Classes ──────────────────────────────────────────────────

class NextGenAMSException(Exception):
    """Base exception for all NextGenAMS errors."""
    def __init__(self, message: str, status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR):
        self.message     = message
        self.status_code = status_code
        super().__init__(message)


class NotFoundError(NextGenAMSException):
    """Resource not found."""
    def __init__(self, resource: str, identifier: str):
        super().__init__(
            message=f"{resource} not found: {identifier}",
            status_code=status.HTTP_404_NOT_FOUND
        )


class UnauthorizedError(NextGenAMSException):
    """Authentication failed."""
    def __init__(self, detail: str = "Authentication required"):
        super().__init__(
            message=detail,
            status_code=status.HTTP_401_UNAUTHORIZED
        )


class ForbiddenError(NextGenAMSException):
    """Authorisation failed — user authenticated but lacks permission."""
    def __init__(self, detail: str = "Insufficient permissions"):
        super().__init__(
            message=detail,
            status_code=status.HTTP_403_FORBIDDEN
        )


class ValidationError(NextGenAMSException):
    """Business rule validation failed."""
    def __init__(self, detail: str):
        super().__init__(
            message=detail,
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY
        )


class ExternalServiceError(NextGenAMSException):
    """External service call failed — Vector API, GenAI, ServiceNow etc."""
    def __init__(self, service: str, detail: str):
        super().__init__(
            message=f"{service} error: {detail}",
            status_code=status.HTTP_502_BAD_GATEWAY
        )


class AgentError(NextGenAMSException):
    """LangGraph agent execution failed."""
    def __init__(self, agent: str, detail: str):
        super().__init__(
            message=f"Agent [{agent}] failed: {detail}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


# ── Exception Handlers ────────────────────────────────────────────────────────

def _error_response(status_code: int, message: str, detail: str | None = None) -> JSONResponse:
    """Standard error response format across the entire API."""
    content: dict = {"error": message}
    if detail:
        content["detail"] = detail
    return JSONResponse(status_code=status_code, content=content)


def register_exception_handlers(app: FastAPI) -> None:
    """
    Registers all exception handlers on the FastAPI app.
    Called once inside create_app() factory.
    """

    @app.exception_handler(NextGenAMSException)
    async def nextgenams_exception_handler(
        request: Request,
        exc: NextGenAMSException
    ) -> JSONResponse:
        logger.error(
            f"[{exc.__class__.__name__}] {exc.message} "
            f"| path={request.url.path}"
        )
        return _error_response(exc.status_code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request,
        exc: RequestValidationError
    ) -> JSONResponse:
        logger.warning(f"Request validation error | path={request.url.path} | {exc.errors()}")
        return _error_response(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            message="Request validation failed",
            detail=str(exc.errors())
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request,
        exc: Exception
    ) -> JSONResponse:
        logger.exception(
            f"Unhandled exception | path={request.url.path} | {str(exc)}"
        )
        return _error_response(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            message="An unexpected error occurred. Please try again."
        )