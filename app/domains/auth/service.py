# app/domains/auth/service.py
# JWT token validation service.
#
# Design decisions:
#   - PyJWKClient is a singleton — created once at app startup via app.state
#     Its internal JWK Set cache (cache_jwk_set=True, lifespan=3600) avoids
#     hitting the JWKS endpoint on every request.
#   - cache_keys=False — avoids indefinite caching of potentially revoked keys
#     (PyJWT issue #1051 — lru_cache has no TTL on individual keys)
#   - PyJWKClient uses urllib (sync) — FastAPI runs it in a thread pool automatically
#     when called from async context via run_in_executor (handled by service layer)
#   - AUTH_ENABLED=False returns a mock user for local dev — never use in production
#
# Reusable across ALL NextGenAMS microservices — just copy this domain folder.

import base64
import json
import logging

import jwt
from jwt import PyJWKClient

from app.config import settings
from app.domains.auth.schemas import UserClaims
from app.exceptions import UnauthorizedError

logger = logging.getLogger(__name__)


class AuthService:
    """
    Handles JWT validation using PyJWT + JWKS.
    One instance created at startup and stored in app.state.
    Injected via get_auth_service() dependency.
    """

    def __init__(self):
        self._jwks_client: PyJWKClient | None = None
        self._initialised: bool = False

    def initialise(self) -> None:
        """
        Creates PyJWKClient singleton.
        Called once at FastAPI startup in lifespan.
        Skip if AUTH_ENABLED=False (local dev).
        """
        if not settings.AUTH_ENABLED:
            logger.warning(
                "AUTH_ENABLED=False — JWT validation disabled. "
                "NEVER use this setting in production."
            )
            self._initialised = True
            return

        if not settings.AUTH_JWKS_URL:
            raise RuntimeError(
                "AUTH_JWKS_URL is not configured. "
                "Set it in .env or disable auth with AUTH_ENABLED=False for local dev."
            )

        # Two-tier caching:
        # cache_jwk_set=True  → caches entire JWKS response for lifespan seconds
        # cache_keys=False    → avoids indefinite caching of individual keys (PyJWT #1051)
        self._jwks_client = PyJWKClient(
            uri=settings.AUTH_JWKS_URL,
            cache_jwk_set=True,
            lifespan=3600,       # refresh JWKS every 1 hour
            cache_keys=False,    # no indefinite per-key caching
            timeout=10,
        )
        self._initialised = True
        logger.info(f"AuthService initialised — JWKS: {settings.AUTH_JWKS_URL}")

    def validate_token(self, token: str) -> UserClaims:
        """
        Validates JWT signature, expiry, and audience.
        Returns UserClaims with decoded user identity.
        Raises UnauthorizedError on any failure.

        Note: PyJWKClient.get_signing_key_from_jwt() is synchronous (uses urllib).
        FastAPI automatically runs sync dependencies in a thread pool — no blocking.
        """
        if not self._initialised:
            raise RuntimeError("AuthService not initialised. Call initialise() first.")

        # ── Dev mode — return mock user ───────────────────────────────────────
        if not settings.AUTH_ENABLED:
            return UserClaims(
                user_id="dev-user-001",
                email="dev@pwc.com",
                name="Dev User",
                roles=["user"]
            )

        # ── Validate JWT ──────────────────────────────────────────────────────
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)

            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=[settings.AUTH_ALGORITHM],
                audience=settings.AUTH_AUDIENCE,
                options={"verify_exp": True}
            )

        except jwt.ExpiredSignatureError:
            raise UnauthorizedError("Token has expired")

        except jwt.InvalidAudienceError:
            raise UnauthorizedError("Token audience mismatch")

        except jwt.InvalidTokenError as e:
            raise UnauthorizedError(f"Invalid token: {str(e)}")

        except Exception as e:
            logger.error(f"Unexpected auth error: {str(e)}")
            raise UnauthorizedError("Token validation failed")

        # ── Extract user identity ─────────────────────────────────────────────
        user_id = (
            payload.get(settings.USER_ID_CLAIM)
            or payload.get("sub")
            or payload.get("uid")
        )

        if not user_id:
            raise UnauthorizedError(
                f"No user identifier found in token. "
                f"Expected claim: '{settings.USER_ID_CLAIM}'. "
                f"Run /api/v1/auth/debug/token-claims to inspect your token."
            )

        return UserClaims(
            user_id=str(user_id),
            email=str(payload.get(settings.USER_EMAIL_CLAIM, "")),
            name=str(payload.get(settings.USER_NAME_CLAIM, "")),
            roles=payload.get(settings.USER_ROLES_CLAIM, [])
        )

    def decode_token_unverified(self, token: str) -> dict:
        """
        Decodes JWT payload WITHOUT signature verification.
        Used ONLY by the debug endpoint in dev to inspect claim field names.
        NEVER use this for actual authentication.
        """
        try:
            parts = token.split(".")
            payload_b64 = parts[1] + "=="   # pad for base64
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            return payload
        except Exception as e:
            raise UnauthorizedError(f"Could not decode token: {str(e)}")


# ── Singleton ─────────────────────────────────────────────────────────────────
# Created once — initialise() called in lifespan startup.
auth_service = AuthService()