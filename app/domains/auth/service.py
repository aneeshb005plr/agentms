# app/domains/auth/service.py
# Multi-issuer JWT validation — production-grade, config-driven.
#
# Design principles:
#   - Zero code changes when PwC adds a new identity provider
#   - Each trusted issuer defined in AUTH_ISSUERS config (JSON array)
#   - Unknown issuer = 401 immediately (fail closed)
#   - PyJWT handles: signature, expiry (exp), not-before (nbf), structure, algorithm
#   - We handle: issuer matching, tenant ID (tid), app ID (appid), user ID extraction
#
# Supported issuers:
#   OpenAM  — local dev, PwC internal IdP (no tid/appid claims)
#   Entra   — deployed on DocAssist infra (has tid + appid claims)
#   Any future IdP — just add entry to AUTH_ISSUERS config
#
# Token claim mapping:
#   user_id     ← uid (PwC internal, OpenAM) → oid (Entra) → sub (fallback)
#   email       ← email → preferred_username
#   tenant_id   ← tid (Entra) → realm (OpenAM)

import base64
import json
import logging
from dataclasses import dataclass

import jwt
from jwt import PyJWKClient

from app.config import settings
from app.domains.auth.schemas import UserClaims
from app.exceptions import UnauthorizedError

logger = logging.getLogger(__name__)


@dataclass
class IssuerConfig:
    """
    Configuration for a single trusted identity provider.
    Parsed from one entry in AUTH_ISSUERS JSON array.
    """
    name:           str              # human label for logging
    issuer_pattern: str              # substring matched against token iss claim
    jwks_url:       str              # JWKS endpoint for signing key verification
    verify_tid:     bool = False     # validate tid claim (Entra yes, OpenAM no)
    tid:            str  = ""        # expected tenant ID
    verify_appid:   bool = False     # validate appid claim (Entra yes, OpenAM no)
    appid:          str  = ""        # expected app ID


class AuthService:
    """
    Multi-issuer JWT validation service.
    One JWKS client per trusted issuer — independent key caches.
    Singleton — created once, initialised at FastAPI startup.
    """

    def __init__(self):
        # Maps issuer_pattern → (IssuerConfig, PyJWKClient)
        self._issuers: list[tuple[IssuerConfig, PyJWKClient]] = []
        self._initialised: bool = False

    def initialise(self) -> None:
        """
        Called once at FastAPI startup in lifespan.
        Parses AUTH_ISSUERS and creates one PyJWKClient per issuer.
        Falls back to legacy AUTH_JWKS_URL if AUTH_ISSUERS is empty.
        """
        if not settings.AUTH_ENABLED:
            logger.warning(
                "AUTH_ENABLED=False — JWT validation disabled. "
                "NEVER use in production."
            )
            self._initialised = True
            return

        # ── Parse AUTH_ISSUERS ────────────────────────────────────────────────
        issuers_raw: list[dict] = []
        try:
            parsed = json.loads(settings.AUTH_ISSUERS)
            if isinstance(parsed, list) and len(parsed) > 0:
                issuers_raw = parsed
        except (json.JSONDecodeError, Exception) as e:
            logger.warning("Could not parse AUTH_ISSUERS: %s", str(e))

        # ── Fallback: legacy single-issuer via AUTH_JWKS_URL ─────────────────
        if not issuers_raw and settings.AUTH_JWKS_URL:
            logger.info(
                "AUTH_ISSUERS empty — falling back to legacy AUTH_JWKS_URL: %s",
                settings.AUTH_JWKS_URL,
            )
            issuers_raw = [{
                "name":           "legacy",
                "issuer_pattern": "",        # matches any issuer
                "jwks_url":       settings.AUTH_JWKS_URL,
                "verify_tid":     False,
                "verify_appid":   False,
            }]

        if not issuers_raw:
            raise RuntimeError(
                "No trusted issuers configured. "
                "Set AUTH_ISSUERS (JSON array) or legacy AUTH_JWKS_URL. "
                "See config.py for examples."
            )

        # ── Build one PyJWKClient per issuer ──────────────────────────────────
        for entry in issuers_raw:
            try:
                config = IssuerConfig(
                    name           = entry["name"],
                    issuer_pattern = entry["issuer_pattern"],
                    jwks_url       = entry["jwks_url"],
                    verify_tid     = bool(entry.get("verify_tid", False)),
                    tid            = str(entry.get("tid", "")),
                    verify_appid   = bool(entry.get("verify_appid", False)),
                    appid          = str(entry.get("appid", "")),
                )

                client = PyJWKClient(
                    uri           = config.jwks_url,
                    cache_jwk_set = True,
                    lifespan      = 3600,    # refresh JWKS every 1 hour
                    cache_keys    = False,   # avoids indefinite per-key caching (PyJWT #1051)
                    timeout       = 10,
                )

                self._issuers.append((config, client))
                logger.info(
                    "Trusted issuer registered: name=%s pattern=%s jwks=%s "
                    "verify_tid=%s verify_appid=%s",
                    config.name, config.issuer_pattern or "(any)",
                    config.jwks_url, config.verify_tid, config.verify_appid,
                )

            except KeyError as e:
                raise RuntimeError(
                    f"AUTH_ISSUERS entry missing required field: {e}. "
                    f"Entry: {entry}"
                )

        self._initialised = True
        logger.info(
            "AuthService initialised — %d trusted issuer(s) configured",
            len(self._issuers),
        )

    def validate_token(self, token: str) -> UserClaims:
        """
        Validates JWT and returns UserClaims.

        Validation steps:
          1. Peek at iss claim (unverified) to find matching issuer config
          2. No match → 401 (untrusted issuer — fail closed)
          3. Verify signature using that issuer's JWKS public key
          4. PyJWT verifies: exp, nbf, structure, algorithm
          5. Validate tid if configured (Entra only)
          6. Validate appid if configured (Entra only)
          7. Extract user_id (uid → oid → sub)

        Raises UnauthorizedError on any failure.
        """
        if not self._initialised:
            raise RuntimeError("AuthService not initialised. Call initialise() first.")

        # ── Dev mode ──────────────────────────────────────────────────────────
        if not settings.AUTH_ENABLED:
            return UserClaims(
                user_id    = "devuser001",
                email      = "dev@pwc.com",
                name       = "Dev User (US)",
                given_name = "Dev",
                family_name= "User",
                oid        = "00000000-0000-0000-0000-000000000001",
                sid        = "00000000-0000-0000-0000-000000000002",
                tenant_id  = "dev-tenant",
                roles      = [],
            )

        # ── Step 1: Peek at iss to find matching issuer ───────────────────────
        # Unverified decode — only to read iss for routing to correct JWKS
        # Signature is NOT trusted yet at this point
        try:
            unverified = jwt.decode(
                token,
                options={
                    "verify_signature": False,  # no verification — just reading iss
                    "verify_exp":       False,
                },
                algorithms=[settings.AUTH_ALGORITHM],
            )
        except Exception:
            raise UnauthorizedError("Malformed token — could not read header")

        token_iss = unverified.get("iss", "")
        if not token_iss:
            raise UnauthorizedError("Token missing 'iss' (issuer) claim")

        # ── Step 2: Find matching trusted issuer ──────────────────────────────
        matched_config: IssuerConfig | None = None
        matched_client: PyJWKClient | None  = None

        for config, client in self._issuers:
            # Empty pattern matches any issuer (legacy fallback mode)
            if not config.issuer_pattern or config.issuer_pattern in token_iss:
                matched_config = config
                matched_client = client
                break

        if matched_config is None:
            logger.warning(
                "Token rejected — untrusted issuer: %s. "
                "Trusted patterns: %s",
                token_iss,
                [c.issuer_pattern for c, _ in self._issuers],
            )
            raise UnauthorizedError(
                f"Token issuer '{token_iss}' is not trusted. "
                "Contact the NextGenAMS team to add this identity provider."
            )

        logger.debug(
            "Token matched issuer: %s (pattern: %s)",
            matched_config.name, matched_config.issuer_pattern,
        )

        # ── Step 3-4: Verify signature + exp + nbf + structure ───────────────
        try:
            signing_key = matched_client.get_signing_key_from_jwt(token)
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=[settings.AUTH_ALGORITHM],
                options={
                    "verify_exp": True,    # enforce expiry
                    "verify_nbf": True,    # enforce not-before
                    "verify_aud": False,   # skip aud — OIDC tokens use Graph audience
                                           # We validate identity via tid + appid instead
                },
            )
        except jwt.ExpiredSignatureError:
            raise UnauthorizedError("Token has expired — please log in again")
        except jwt.ImmatureSignatureError:
            raise UnauthorizedError("Token not yet valid (nbf claim)")
        except jwt.InvalidSignatureError:
            raise UnauthorizedError("Token signature invalid — possible tampering")
        except jwt.DecodeError:
            raise UnauthorizedError("Token structure invalid")
        except jwt.InvalidTokenError as e:
            raise UnauthorizedError(f"Token validation failed: {str(e)}")
        except Exception as e:
            logger.error(
                "Unexpected error validating token for issuer %s: %s",
                matched_config.name, str(e),
            )
            raise UnauthorizedError("Token validation failed — please try again")

        # ── Step 5: Validate tenant ID (Entra only) ───────────────────────────
        if matched_config.verify_tid:
            token_tid = payload.get("tid", "")
            if not token_tid:
                raise UnauthorizedError(
                    f"Token from issuer '{matched_config.name}' missing 'tid' claim"
                )
            if token_tid != matched_config.tid:
                raise UnauthorizedError(
                    f"Token tenant mismatch for issuer '{matched_config.name}'. "
                    f"Expected: {matched_config.tid}, Got: {token_tid}"
                )

        # ── Step 6: Validate app ID (Entra only) ─────────────────────────────
        if matched_config.verify_appid:
            # appid = Entra v1.0, azp = Entra v2.0 / standard OIDC
            token_appid = payload.get("appid") or payload.get("azp", "")
            if not token_appid:
                raise UnauthorizedError(
                    f"Token from issuer '{matched_config.name}' "
                    "missing 'appid'/'azp' claim"
                )
            if token_appid != matched_config.appid:
                raise UnauthorizedError(
                    f"Token app mismatch for issuer '{matched_config.name}'. "
                    f"Expected: {matched_config.appid}, Got: {token_appid}"
                )

        # ── Step 7: Extract user identity ────────────────────────────────────
        # uid  = PwC internal ID (e.g. abahuleyan001) — OpenAM primary
        # oid  = Entra object ID — Entra primary
        # sub  = subject — universal fallback
        user_id = (
            payload.get("uid")
            or payload.get("oid")
            or payload.get("sub")
        )

        if not user_id:
            raise UnauthorizedError(
                f"Token from issuer '{matched_config.name}' has no user identifier. "
                "Expected 'uid', 'oid', or 'sub' claim."
            )

        logger.debug(
            "Token validated — issuer=%s user=%s",
            matched_config.name, str(user_id),
        )

        return UserClaims(
            user_id     = str(user_id),
            email       = str(payload.get("email") or payload.get("preferred_username") or payload.get("unique_name", "")),
            name        = str(payload.get("name", "")),
            given_name  = str(payload.get("given_name", "")),
            family_name = str(payload.get("family_name", "")),
            oid         = str(payload.get("oid", "")),
            sid         = str(payload.get("sid", "")),
            tenant_id   = str(payload.get("tid") or payload.get("realm", "")),
            roles       = payload.get("roles", []),
        )

    def decode_token_unverified(self, token: str) -> dict:
        """Dev/debug only — decodes JWT without verification to inspect claims."""
        try:
            parts       = token.split(".")
            payload_b64 = parts[1] + "=="
            return json.loads(base64.urlsafe_b64decode(payload_b64))
        except Exception as e:
            raise UnauthorizedError(f"Could not decode token: {str(e)}")


# Singleton — imported by dependencies.py
auth_service = AuthService()