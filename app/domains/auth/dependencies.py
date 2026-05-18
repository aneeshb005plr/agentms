# app/domains/auth/dependencies.py

from typing import Annotated
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings
from app.domains.auth.schemas import UserClaims
from app.domains.auth.service import auth_service

# auto_error=False when AUTH_ENABLED=False — allows requests without token
# auto_error=True when AUTH_ENABLED=True  — enforces token presence
_security = HTTPBearer(auto_error=settings.AUTH_ENABLED)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_security),
) -> UserClaims:
    """
    Validates JWT and returns UserClaims.

    AUTH_ENABLED=False (local dev):
        - No Authorization header needed
        - credentials will be None
        - Returns mock user automatically

    AUTH_ENABLED=True (staging/prod):
        - Authorization header required
        - Raises HTTP 401 if missing or invalid
    """
    if not settings.AUTH_ENABLED:
        # Dev mode — return mock user without any token
        return auth_service.validate_token("")

    # Production — validate real token
    return auth_service.validate_token(credentials.credentials)


# Convenience alias — use this in all routers
CurrentUser = Annotated[UserClaims, Depends(get_current_user)]