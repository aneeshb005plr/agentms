# app/domains/auth/service.py
# JWT validation via PyJWT + JWKS.
#
# XYZ Entra ID token claim mapping:
#   user_id     ← uid   (XYZ internal ID — primary identifier)
#   oid         ← oid   (Entra object ID — kept as reference)
#   email       ← email
#   name        ← name
#   given_name  ← given_name
#   family_name ← family_name
#   sid         ← sid   (session ID)
#   tenant_id   ← tid
#   roles       ← roles (not in current token — defaults to [])
#
# JWKS URL (Entra ID v2.0):
#   https://login.microsoftonline.com/{tenant_id}/v2.0/keys
#
# PyJWKClient:
#   cache_jwk_set=True, lifespan=3600 — refreshes JWKS every 1 hour
#   cache_keys=False — avoids indefinite per-key caching (PyJWT issue #1051)

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
    """JWT validation service — one instance for entire service lifetime."""

    def __init__(self):
        self._jwks_client: PyJWKClient | None = None
        self._initialised: bool = False

    def initialise(self) -> None:
        """Called once at FastAPI startup in lifespan."""
        if not settings.AUTH_ENABLED:
            logger.warning(
                "AUTH_ENABLED=False — JWT validation disabled. "
                "NEVER use in production."
            )
            self._initialised = True
            return

        if not settings.AUTH_JWKS_URL:
            raise RuntimeError(
                "AUTH_JWKS_URL not configured. "
                "For XYZ Entra ID set: "
                "https://login.microsoftonline.com/{tenant_id}/v2.0/keys"
            )

        self._jwks_client = PyJWKClient(
            uri=settings.AUTH_JWKS_URL,
            cache_jwk_set=True,
            lifespan=3600,     # refresh JWKS every 1 hour
            cache_keys=False,  # avoids indefinite per-key caching (PyJWT #1051)
            timeout=10,
        )
        self._initialised = True
        logger.info("AuthService initialised — JWKS: %s", settings.AUTH_JWKS_URL)

    def validate_token(self, token: str) -> UserClaims:
        """
        Validates JWT and returns UserClaims.
        Raises UnauthorizedError on any failure.
        """
        if not self._initialised:
            raise RuntimeError("AuthService not initialised.")

        # Dev mode — return mock user (AUTH_ENABLED=False)
        # Token can be empty string or any value — not validated
        if not settings.AUTH_ENABLED:
            return UserClaims(
                user_id="devuser001",
                email="dev@XYZ.com",
                name="Dev User (US)",
                given_name="Dev",
                family_name="User",
                oid="00000000-0000-0000-0000-000000000001",
                sid="00000000-0000-0000-0000-000000000002",
                tenant_id="dev-tenant",
                roles=[],
            )

        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=[settings.AUTH_ALGORITHM],
                audience=settings.AUTH_AUDIENCE,
                options={"verify_exp": True},
            )
        except jwt.ExpiredSignatureError:
            raise UnauthorizedError("Token has expired")
        except jwt.InvalidAudienceError:
            raise UnauthorizedError("Token audience mismatch")
        except jwt.InvalidTokenError as e:
            raise UnauthorizedError(f"Invalid token: {str(e)}")
        except Exception as e:
            logger.error("Unexpected auth error: %s", str(e))
            raise UnauthorizedError("Token validation failed")

        # uid = XYZ internal ID (e.g. abahuleyan001) — preferred primary key
        # Fall back to oid (Entra object ID) if uid not present
        # Fall back to sub as last resort
        user_id = (
            payload.get("uid")
            or payload.get("oid")
            or payload.get("sub")
        )

        if not user_id:
            raise UnauthorizedError(
                "No user identifier found in token. "
                "Expected 'uid', 'oid', or 'sub' claim. "
                "Run /api/v1/auth/debug/token-claims to inspect your token."
            )

        return UserClaims(
            user_id=str(user_id),
            email=str(payload.get("email") or payload.get("preferred_username", "")),
            name=str(payload.get("name", "")),
            given_name=str(payload.get("given_name", "")),
            family_name=str(payload.get("family_name", "")),
            oid=str(payload.get("oid", "")),           # Entra ID only — empty for OpenAM
            sid=str(payload.get("sid", "")),
            tenant_id=str(payload.get("tid") or payload.get("realm", "")),  # tid=Entra, realm=OpenAM
            roles=payload.get("roles", []),             # not in current token — defaults to []
        )

    def decode_token_unverified(self, token: str) -> dict:
        """Dev only — decodes JWT without verification to inspect all claims."""
        try:
            parts       = token.split(".")
            payload_b64 = parts[1] + "=="
            return json.loads(base64.urlsafe_b64decode(payload_b64))
        except Exception as e:
            raise UnauthorizedError(f"Could not decode token: {str(e)}")


# Singleton
auth_service = AuthService()