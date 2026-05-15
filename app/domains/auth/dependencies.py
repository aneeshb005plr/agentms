# app/domains/auth/dependencies.py
# FastAPI dependency for JWT authentication.
# Import get_current_user in any router that needs a protected endpoint.
#
# Usage in any router:
#   from app.domains.auth.dependencies import CurrentUser
#
#   @router.get("/something")
#   async def my_endpoint(user: CurrentUser):
#       # user.user_id, user.email, user.name, user.roles available
#       ...

from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.domains.auth.schemas import UserClaims
from app.domains.auth.service import auth_service

# HTTPBearer extracts the Bearer token from Authorization header automatically
_security = HTTPBearer(auto_error=True)


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_security)
) -> UserClaims:
    """
    FastAPI dependency — validates JWT and returns UserClaims.
    Raises HTTP 401 automatically on missing/invalid/expired token.
    Runs synchronously — FastAPI handles thread pool execution.

    Inject this in any endpoint that requires authentication:
        async def endpoint(user: CurrentUser): ...
    """
    return auth_service.validate_token(credentials.credentials)


# ── Convenience type alias ────────────────────────────────────────────────────
# Cleaner router signatures — no need to repeat Depends() everywhere
CurrentUser = Annotated[UserClaims, Depends(get_current_user)]