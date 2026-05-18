# app/api/v1/auth.py

import logging
from fastapi import APIRouter, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi import Depends

from app.config import settings
from app.domains.auth.dependencies import CurrentUser
from app.domains.auth.schemas import UserClaims, TokenDebugResponse
from app.domains.auth.service import auth_service
from app.exceptions import ForbiddenError

logger = logging.getLogger(__name__)
router   = APIRouter(prefix="/auth", tags=["Auth"])
_security = HTTPBearer(auto_error=settings.AUTH_ENABLED)


@router.get("/me", response_model=UserClaims)
async def get_me(user: CurrentUser) -> UserClaims:
    """Returns current authenticated user's claims."""
    return user


@router.get("/debug/token-claims", response_model=TokenDebugResponse)
async def debug_token_claims(
    credentials: HTTPAuthorizationCredentials = Depends(_security),
) -> TokenDebugResponse:
    """
    DEV ONLY — Returns all raw JWT claims without verification.
    Use once to find correct claim field names for your IdP.
    Disabled in production.
    """
    if settings.IS_PRODUCTION:
        raise ForbiddenError("Debug endpoints are disabled in production")

    logger.warning("Debug token-claims endpoint called — remove before production")
    all_claims = auth_service.decode_token_unverified(credentials.credentials)
    return TokenDebugResponse(all_claims=all_claims)