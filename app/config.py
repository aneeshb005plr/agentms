# app/config.py
# Single source of truth for ALL settings and environment variables.
# Nothing is hardcoded anywhere else in the codebase.
# All values come from environment variables - see .env.example
#
# Source precedence: secrets_dir > env vars > .env file > defaults
#
# PROMPTS ARE NOT HERE.
# Prompts live in MongoDB 'prompts' collection - managed via Admin UI.
# Fallback prompts live in app/domains/prompts/defaults.py

import logging
import os

from pydantic import Field, SecretStr, computed_field, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

DEFAULT_SECRETS_PATH: str = os.environ.get("SECRETS_VOLUME_PATH", "/var/app/secrets")

# Configure logging at import time using env var directly.
# Settings haven't been loaded yet — basicConfig must run before any logger call.
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)

logger = logging.getLogger("app.config")


class Settings(BaseSettings):
    """Application settings — single source of truth."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        secrets_dir=DEFAULT_SECRETS_PATH if os.path.isdir(DEFAULT_SECRETS_PATH) else None,
        case_sensitive=True,
        extra="ignore",
    )

    # ── Service Identity ──────────────────────────────────────────────────────
    SERVICE_NAME:        str = "nextgenams-agent-engine"
    SERVICE_VERSION:     str = "1.0.0"
    SERVICE_DESCRIPTION: str = "NextGenAMS AI Agent Engine - PwC IT Support Automation"
    ENVIRONMENT:         str = "development"
    DEBUG:               bool = False
    SECRETS_VOLUME_PATH: str = DEFAULT_SECRETS_PATH

    # ── Server ────────────────────────────────────────────────────────────────
    PORT: int = 8080

    # ── GenAI Shared Service (OpenAI compatible) ──────────────────────────────
    # TBC from PwC team
    GENAI_BASE_URL:    str | None       = None
    GENAI_API_KEY:     SecretStr | None = None
    GENAI_MODEL_SMART: str                 = "gpt-4o"       # complex reasoning
    GENAI_MODEL_FAST:  str                 = "gpt-4o-mini"  # quick tasks
    GENAI_TEMPERATURE: float               = 0.0
    GENAI_MAX_TOKENS:  int                 = 2000

    # ── Vector API (Knowledge Base Search) ───────────────────────────────────
    # TBC from PwC team — URL, auth, request/response format
    # Confirmed endpoint:
    # POST https://webapp-docassist.east.dev.ngc.pwcinternal.com
    #      /api/vector-retrieval/api/v1/nextgenams_dev/query
    # Request:  { "question": str, "top_k": int }
    # Response: { "question", "answer", "chunks": [...], "total_chunks" }
    # Each chunk has: text, score, source_url, file_name, metadata
    # metadata includes: application, is_general, rerank_score
    VECTOR_API_URL: str | None       = None
    VECTOR_API_KEY: SecretStr | None = None  # Optional — Vector API may not require auth
    VECTOR_TOP_K:   int                 = 15

    # Quality gate — chunks below this rerank_score are ignored
    # All chunks below threshold → no relevant info → suggest ticket gently
    VECTOR_RERANK_SCORE_THRESHOLD:        float = 0.5

    # Minimum chunks from same app to set app_identified
    # Below this — mixed results → app_identified = None
    VECTOR_APP_IDENTIFICATION_MIN_CHUNKS: int   = 2

    # ── ServiceNow ────────────────────────────────────────────────────────────
    # Phase 1 — manual link only. Full API in Phase 2.
    SERVICENOW_TICKET_URL: str | None = None

    # ── MongoDB ───────────────────────────────────────────────────────────────
    MONGODB_URI:                str = "mongodb://localhost:27017"
    MONGODB_DB_NAME:            str = "nextgenams"
    MONGODB_PROMPTS_COLLECTION: str = "prompts"

    # ── Redis ─────────────────────────────────────────────────────────────────
    # Used for prompt cache invalidation across AKS pods
    REDIS_URL:            str = "redis://localhost:6379"
    REDIS_PROMPT_CHANNEL: str = "nextgenams:prompt_invalidated"

    # ── Auth — JWT Validation ─────────────────────────────────────────────────
    # AUTH_ENABLED=False for local dev only — ALWAYS True in staging/production
    AUTH_ENABLED:   bool = False
    AUTH_ALGORITHM: str  = "RS256"

    # ── Multi-issuer configuration (production-grade) ─────────────────────────
    # Config-driven trusted issuers list — adding a new IdP = add config entry,
    # zero code changes required.
    #
    # Each entry is a JSON object with:
    #   name           — human label for logging/debugging
    #   issuer_pattern — substring matched against token "iss" claim
    #   jwks_url       — JWKS endpoint for this issuer's signing keys
    #   verify_tid     — validate "tid" claim (Entra yes, OpenAM no)
    #   tid            — expected tenant ID (required if verify_tid=true)
    #   verify_appid   — validate "appid" claim (Entra yes, OpenAM no)
    #   appid          — expected app ID (required if verify_appid=true)
    #
    # ── Local dev (OpenAM only) ───────────────────────────────────────────────
    # AUTH_ISSUERS=[
    #   {"name":"openam","issuer_pattern":"pwcinternal.com",
    #    "jwks_url":"https://login-stg.pwcinternal.com:443/openam/oauth2/keys",
    #    "verify_tid":false,"verify_appid":false}
    # ]
    #
    # ── Deployed on DocAssist infra (Entra only) ──────────────────────────────
    # AUTH_ISSUERS=[
    #   {"name":"entra","issuer_pattern":"sts.windows.net",
    #    "jwks_url":"https://login.microsoftonline.com/831f8b7b-.../v2.0/keys",
    #    "verify_tid":true,"tid":"831f8b7b-7bb6-4d34-a62b-7baf9792d24a",
    #    "verify_appid":true,"appid":"1ecf0f21-8bd3-4ca1-a7e7-29aec744bc9f"}
    # ]
    #
    # ── Both issuers (staging — supports both token types) ────────────────────
    # AUTH_ISSUERS=[{openam entry},{entra entry}]
    #
    # When a new IdP is added by PwC — just append a new entry. No code change.
    #
    AUTH_ISSUERS: str = "[]"   # JSON string — parsed into list at startup

    # Legacy fields — kept for backward compatibility with existing deployments
    # Ignored when AUTH_ISSUERS contains a non-empty array
    AUTH_JWKS_URL: str | None = None
    AUTH_AUDIENCE: str | None = None

    # ── JWT Claim Mapping — confirmed PwC Entra ID v2.0 token ─────────────────
    # Same claims confirmed for both Entra ID and OpenAM:
    #   uid          = PwC internal user ID (e.g. abahuleyan001) — PRIMARY
    #   oid          = Entra object ID (UUID) — fallback
    #   sub          = subject — last resort fallback
    #   email        = user email
    #   name         = display name (e.g. Aneesh Bahuleyan (US))
    #   given_name   = first name
    #   family_name  = last name
    #   sid          = session ID
    #   tid          = tenant ID
    #   roles        = not in current token — defaults to []
    # Claim resolution logic lives in app/domains/auth/service.py

    # ── Conversation / Memory ─────────────────────────────────────────────────
    MAX_MESSAGES_IN_CONTEXT:    int = 10   # trim after this many messages (Layer 1)
    CONVERSATION_HISTORY_LIMIT: int = 50   # sidebar conversation list limit
    SUMMARY_TRIGGER_COUNT:      int = 20   # generate rolling summary after N messages (Layer 2)

    # ── CORS ──────────────────────────────────────────────────────────────────
    # Production: set to Angular app URL e.g. https://nextgenams.pwc.com
    CORS_ORIGINS: list[str] = Field(default_factory=lambda: ["*"])

    # ── Feature Flags ─────────────────────────────────────────────────────────
    ENABLE_SWAGGER: bool = True   # disabled automatically in production

    # ── HTTP Client ───────────────────────────────────────────────────────────
    # Vector API is an internal PwC endpoint — SSL cert not required
    # Default False — no cert verification needed in any environment
    VECTOR_API_VERIFY_SSL: bool = False

    # ── Source precedence ─────────────────────────────────────────────────────
    # secrets volume → env vars → .env file → defaults
    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            file_secret_settings,   # secrets volume — highest priority
            env_settings,
            dotenv_settings,
        )

    # ── Computed fields ───────────────────────────────────────────────────────
    @computed_field
    @property
    def IS_DEVELOPMENT(self) -> bool:
        return self.ENVIRONMENT.lower() == "development"

    @computed_field
    @property
    def IS_TESTING(self) -> bool:
        return self.ENVIRONMENT.lower() == "testing"

    @computed_field
    @property
    def IS_PRODUCTION(self) -> bool:
        return self.ENVIRONMENT.lower() == "production"

    # ── Production validation — fail fast on missing required values ───────────
    @model_validator(mode="after")
    def _validate_required_in_production(self) -> "Settings":
        if not self.IS_PRODUCTION:
            return self

        required = {
            "MONGODB_URI":    self.MONGODB_URI != "mongodb://localhost:27017",
            "GENAI_BASE_URL": bool(self.GENAI_BASE_URL),
            "GENAI_API_KEY":  self.GENAI_API_KEY is not None,
            "VECTOR_API_URL": bool(self.VECTOR_API_URL),
            "AUTH_JWKS_URL":  bool(self.AUTH_JWKS_URL),
            "AUTH_AUDIENCE":  bool(self.AUTH_AUDIENCE),
        }

        missing = [name for name, ok in required.items() if not ok]

        if missing:
            raise ValueError(
                f"Missing required production settings: {', '.join(missing)}"
            )

        if not self.AUTH_ENABLED:
            raise ValueError("AUTH_ENABLED must be True in production")



        return self

    # ── Safe representation for logging ───────────────────────────────────────
    def safe_dump(self) -> dict:
        """
        Returns settings dict safe to log.
        Masks API keys and credentials embedded in connection URIs.
        """
        data = self.model_dump(mode="json")

        # Mask SecretStr fields
        for key in ("GENAI_API_KEY",):
            if data.get(key) is not None:
                data[key] = "***"

        # Mask user:password in URIs
        for url_key in ("MONGODB_URI", "REDIS_URL"):
            url = getattr(self, url_key, None)
            if url and "@" in url:
                try:
                    scheme, rest = url.split("://", 1)
                    _, host_part = rest.split("@", 1)
                    data[url_key] = f"{scheme}://***@{host_part}"
                except ValueError:
                    pass

        return data


# ── Module-level singleton ────────────────────────────────────────────────────
# Import this everywhere — never instantiate Settings() directly
settings = Settings()

logger.debug(
    "Settings loaded for ENVIRONMENT=%s (DEBUG=%s)",
    settings.ENVIRONMENT,
    settings.DEBUG,
)