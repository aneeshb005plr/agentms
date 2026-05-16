# app/domains/auth/dependencies.py

from typing import Annotated
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.domains.auth.schemas import UserClaims
from app.domains.auth.service import auth_service

_security = HTTPBearer(auto_error=True)


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_security),
) -> UserClaims:
    """Validates JWT and returns UserClaims. Raises HTTP 401 on failure."""
    return auth_service.validate_token(credentials.credentials)


# Convenience alias — use this in all routers
CurrentUser = Annotated[UserClaims, Depends(get_current_user)]