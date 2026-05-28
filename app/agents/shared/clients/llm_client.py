# app/agents/shared/clients/llm_client.py
# LLM client — initialises smart and fast LLM instances.
#
# Two models:
#   smart_llm  — gpt-4o     — complex reasoning: synthesiser, response formatter
#   fast_llm   — gpt-4o-mini — quick tasks: intent classifier, query builder
#
# Both point to PwC GenAI shared service (OpenAI-compatible API).
# Singleton — created once at startup, reused across all agent nodes.
#
# Token tracking:
#   stream_usage=True  — ensures usage_metadata available during streaming
#   response.usage_metadata gives: input_tokens, output_tokens, total_tokens
#
# How agent nodes extract token usage:
#   response = await llm.ainvoke(messages)
#   usage = {
#       "agent": AGENT_NAME,
#       "node":  NODE_NAME,
#       "model": response.response_metadata.get("model_name", settings.GENAI_MODEL_SMART),
#       "input_tokens":  response.usage_metadata["input_tokens"],
#       "output_tokens": response.usage_metadata["output_tokens"],
#       "total_tokens":  response.usage_metadata["total_tokens"],
#   }
#   return {"current_message_llm_calls": [usage]}  # operator.add appends

import logging
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from app.config import settings

logger = logging.getLogger(__name__)


class LLMClient:
    """
    Holds smart and fast LLM instances.
    Singleton — initialised once at startup via initialise().
    """

    def __init__(self):
        self._smart: BaseChatModel | None = None
        self._fast:  BaseChatModel | None = None
        self._initialised: bool = False

    def initialise(self) -> None:
        """
        Creates LLM instances.
        Called once in FastAPI lifespan startup.
        Fails fast if GENAI_BASE_URL or GENAI_API_KEY not configured.
        """
        if not settings.GENAI_BASE_URL:
            raise RuntimeError(
                "GENAI_BASE_URL not configured. "
                "Set it in .env or secrets volume."
            )

        if not settings.GENAI_API_KEY:
            raise RuntimeError(
                "GENAI_API_KEY not configured. "
                "Set it in .env or secrets volume."
            )

        api_key = settings.GENAI_API_KEY.get_secret_value()

        # Smart LLM — gpt-4o — complex reasoning
        self._smart = init_chat_model(
            model=settings.GENAI_MODEL_SMART,
            model_provider="openai",
            base_url=settings.GENAI_BASE_URL,
            api_key=api_key,
            temperature=settings.GENAI_TEMPERATURE,
            max_tokens=settings.GENAI_MAX_TOKENS,
            stream_usage=True,      # usage_metadata available during streaming
        )

        # Fast LLM — gpt-4o-mini — quick classification and rewriting
        self._fast = init_chat_model(
            model=settings.GENAI_MODEL_FAST,
            model_provider="openai",
            base_url=settings.GENAI_BASE_URL,
            api_key=api_key,
            temperature=settings.GENAI_TEMPERATURE,
            max_tokens=settings.GENAI_MAX_TOKENS,
            stream_usage=True,
        )

        self._initialised = True
        logger.info(
            "LLMClient initialised — smart=%s fast=%s base_url=%s",
            settings.GENAI_MODEL_SMART,
            settings.GENAI_MODEL_FAST,
            settings.GENAI_BASE_URL,
        )

    @property
    def smart(self) -> BaseChatModel:
        """
        Returns smart LLM — gpt-4o.
        Use for: reasoning_synthesiser, response_formatter.
        """
        if not self._initialised or not self._smart:
            raise RuntimeError("LLMClient not initialised. Call initialise() first.")
        return self._smart

    @property
    def fast(self) -> BaseChatModel:
        """
        Returns fast LLM — gpt-4o-mini.
        Use for: intent_classifier, query_builder.
        """
        if not self._initialised or not self._fast:
            raise RuntimeError("LLMClient not initialised. Call initialise() first.")
        return self._fast


# Singleton — imported directly by agent nodes
llm_client = LLMClient()