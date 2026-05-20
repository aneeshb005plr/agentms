# app/domains/prompts/cache.py
# In-memory singleton cache for agent prompts.
# Loaded at startup from MongoDB. Invalidated via Redis pub/sub.
#
# Fallback chain:
#   1. In-memory cache  → microseconds
#   2. MongoDB          → on cache miss
#   3. defaults.py      → first boot only

import logging
from typing import Any

logger = logging.getLogger(__name__)


class PromptCache:
    """In-memory prompt cache singleton."""

    # ── Agent ID constants ────────────────────────────────────────────────────
    CONVERSATIONAL_SUPPORT_AGENT = "conversational_support_agent"
    TITLE_GENERATION             = "title_generation"
    SUGGESTION_GENERATION        = "suggestion_generation"

    # ── Prompt key constants ──────────────────────────────────────────────────
    SYSTEM_PROMPT        = "system_prompt"
    QUERY_REWRITE        = "query_rewrite"
    TITLE_PROMPT         = "title_prompt"
    CONVERSATION_SUMMARY = "conversation_summary"
    SUGGESTION_PROMPT    = "suggestion_prompt"

    def __init__(self):
        self._cache: dict[str, str] = {}

    @staticmethod
    def build_key(agent_id: str, prompt_key: str) -> str:
        return f"{agent_id}:{prompt_key}"

    def get(self, agent_id: str, prompt_key: str) -> str | None:
        return self._cache.get(self.build_key(agent_id, prompt_key))

    def set(self, agent_id: str, prompt_key: str, content: str) -> None:
        key = self.build_key(agent_id, prompt_key)
        self._cache[key] = content
        logger.debug("Prompt cached: %s", key)

    def invalidate(self, agent_id: str, prompt_key: str) -> None:
        key = self.build_key(agent_id, prompt_key)
        removed = self._cache.pop(key, None)
        if removed:
            logger.info("Prompt cache invalidated: %s", key)

    def invalidate_all(self) -> None:
        count = len(self._cache)
        self._cache.clear()
        logger.info("Prompt cache cleared — %d entries removed", count)

    def load_many(self, prompts: list[dict[str, Any]]) -> None:
        """Bulk load from MongoDB on startup."""
        for prompt in prompts:
            self.set(
                agent_id=prompt["agent_id"],
                prompt_key=prompt["prompt_key"],
                content=prompt["content"],
            )
        logger.info("Prompt cache loaded — %d prompts", len(prompts))

    def list_keys(self) -> list[str]:
        return list(self._cache.keys())

    def size(self) -> int:
        return len(self._cache)


# Singleton
prompt_cache = PromptCache()