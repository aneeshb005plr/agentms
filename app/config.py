# config.py
# Single source of truth for ALL settings and environment variables.
# Nothing is hardcoded anywhere else in the codebase.
# All values come from environment variables — see .env.example
#
# PROMPTS ARE NOT HERE (primary source).
# Prompts live in MongoDB `prompts` collection — managed via Admin UI.
# The _DEFAULT prompts below are FALLBACKS only — used when MongoDB has no
# entry yet (e.g. first boot). Once admin saves a prompt, MongoDB takes over.

from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):

    # ── Service Identity ──────────────────────────────────────────────────────
    SERVICE_NAME:    str = "nextgenams-agent-engine"
    SERVICE_VERSION: str = "1.0.0"
    ENVIRONMENT:     str = "development"    # development | staging | production
    DEBUG:           bool = False

    # ── GenAI Shared Service (OpenAI Compatible) ──────────────────────────────
    # Provided by PwC team — TBC
    GENAI_BASE_URL:    str   = ""           # e.g. https://your-genai-service/v1
    GENAI_API_KEY:     str   = ""
    GENAI_MODEL_SMART: str   = "gpt-4o"    # Complex reasoning — main agent LLM
    GENAI_MODEL_FAST:  str   = "gpt-4o-mini" # Fast tasks — query rewrite, titles
    GENAI_TEMPERATURE: float = 0.0         # Deterministic responses
    GENAI_MAX_TOKENS:  int   = 2000

    # ── Vector API (Knowledge Base Search) ───────────────────────────────────
    # PwC Vector API — exact request/response format TBC from PwC team
    VECTOR_API_URL: str = ""               # Full endpoint URL
    VECTOR_API_KEY: str = ""               # Auth key if required
    VECTOR_TOP_K:   int = 3                # Number of results to fetch

    # ── ServiceNow ────────────────────────────────────────────────────────────
    # Phase 1 — manual link only. Full API integration in Phase 2.
    SERVICENOW_TICKET_URL: str = ""        # e.g. https://pwc.service-now.com/sp

    # ── MongoDB Atlas ─────────────────────────────────────────────────────────
    MONGODB_URI:                str = ""           # Full Atlas connection string
    MONGODB_DB_NAME:            str = "nextgenams" # Single DB — all collections
    MONGODB_PROMPTS_COLLECTION: str = "prompts"    # Agent prompt store

    # ── Redis ─────────────────────────────────────────────────────────────────
    # Used for: prompt cache invalidation across AKS pods
    # Local dev: redis://localhost:6379
    # Production: Azure Redis Cache connection string
    REDIS_URL:            str = "redis://localhost:6379"
    REDIS_PROMPT_CHANNEL: str = "nextgenams:prompt_invalidated"  # pub/sub channel

    # ── Auth — JWT Validation ─────────────────────────────────────────────────
    # Ocelot does NOT validate tokens. FastAPI validates independently.
    AUTH_ENABLED:   bool = True            # Set False for local dev only
    AUTH_JWKS_URL:  str  = ""             # Entra ID or OpenAM JWKS endpoint
    AUTH_AUDIENCE:  str  = ""             # App Client ID
    AUTH_ALGORITHM: str  = "RS256"

    # ── JWT Claim Mapping ─────────────────────────────────────────────────────
    # Entra ID: USER_ID_CLAIM=oid | OpenAM: USER_ID_CLAIM=sub or uid
    # Run GET /debug/token-claims in dev to find the correct field names
    USER_ID_CLAIM:    str = "oid"
    USER_EMAIL_CLAIM: str = "preferred_username"
    USER_NAME_CLAIM:  str = "name"
    USER_ROLES_CLAIM: str = "roles"

    # ── Conversation Settings ─────────────────────────────────────────────────
    MAX_MESSAGES_IN_CONTEXT:    int = 10   # Trim after this many messages
    CONVERSATION_HISTORY_LIMIT: int = 50   # Max conversations in sidebar

    # ── CORS ──────────────────────────────────────────────────────────────────
    # Development: allow all origins
    # Production: set to Angular app URL e.g. https://nextgenams.pwc.com
    CORS_ORIGINS: list[str] = ["*"]

    # ── Prompt Defaults (FALLBACK ONLY) ───────────────────────────────────────
    # These are used ONLY when MongoDB prompts collection has no entry.
    # Primary source is always MongoDB — managed via Admin UI (Phase 2).
    # To update prompts in production: use Admin UI, not this file.

    CONVERSATIONAL_SUPPORT_AGENT_SYSTEM_PROMPT_DEFAULT: str = (
        "You are NextGenAMS, an intelligent IT support assistant for PwC.\n"
        "Your job is to help users resolve application issues quickly and accurately.\n\n"
        "Rules:\n"
        "- ALWAYS search the knowledge base before answering any question\n"
        "- Give clear, concise, professional responses\n"
        "- If the knowledge base has relevant information, use it to answer\n"
        "- If no relevant answer is found, provide the ServiceNow ticket link\n"
        "- Never guess or make up information about systems or applications\n"
        "- Keep responses focused and actionable"
    )

    QUERY_REWRITE_PROMPT_DEFAULT: str = (
        "Given the conversation history below, rewrite the user's latest message "
        "into a self-contained search query that can be understood without any prior context.\n\n"
        "Rules:\n"
        "- If the message is already standalone, return it unchanged\n"
        "- If it is a follow-up question, incorporate the necessary context from history\n"
        "- Return ONLY the rewritten query, nothing else\n"
        "- Maximum 20 words\n\n"
        "Conversation history:\n"
        "{history}\n\n"
        "Latest user message: {user_message}\n\n"
        "Standalone search query:"
    )

    TITLE_GENERATION_PROMPT_DEFAULT: str = (
        "Summarise this user message as a short conversation title.\n"
        "Maximum 5 words. Return ONLY the title, no punctuation or quotes.\n\n"
        "Message: {message}\n\n"
        "Title:"
    )

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    """
    Returns cached Settings instance.
    lru_cache ensures .env is read only once per process.
    Use get_settings() everywhere — never instantiate Settings() directly.
    """
    return Settings()


# Convenience alias — import this directly across the codebase
settings = get_settings()