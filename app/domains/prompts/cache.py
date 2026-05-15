# core/prompt_cache.py
# In-memory singleton cache for agent prompts.
#
# Why in-memory cache?
# - Prompts are read on EVERY agent invocation (every user message)
# - MongoDB round-trip on every message = unnecessary latency
# - Prompts change rarely (only when admin updates via Admin UI)
# - Cache loads at startup, invalidates via Redis pub/sub when admin updates
#
# Cache key format:  "{agent_id}:{prompt_key}"
# Example:           "conversational_support_agent:system_prompt"
#
# Fallback chain:
#   1. In-memory cache  → fastest
#   2. MongoDB          → on cache miss
#   3. config.py _DEFAULT → if MongoDB has no entry yet (first boot)

import logging
from typing import Any

logger = logging.getLogger(__name__)


class PromptCache:
    """
    Thread-safe in-memory prompt cache.
    Singleton — one instance for the entire service lifetime.
    """

    # ── Prompt key constants ──────────────────────────────────────────────────
    # Use these constants everywhere — never hardcode strings in agent files.

    # Agent IDs
    CONVERSATIONAL_SUPPORT_AGENT = "conversational_support_agent"
    TITLE_GENERATION              = "title_generation"

    # Prompt keys
    SYSTEM_PROMPT   = "system_prompt"
    QUERY_REWRITE   = "query_rewrite"
    TITLE_PROMPT    = "title_prompt"

    def __init__(self):
        self._cache: dict[str, str] = {}

    # ── Cache key builder ─────────────────────────────────────────────────────

    @staticmethod
    def build_key(agent_id: str, prompt_key: str) -> str:
        """Builds the cache lookup key."""
        return f"{agent_id}:{prompt_key}"

    # ── Get ───────────────────────────────────────────────────────────────────

    def get(self, agent_id: str, prompt_key: str) -> str | None:
        """
        Returns cached prompt string or None if not in cache.
        None triggers a MongoDB lookup in PromptService.
        """
        key = self.build_key(agent_id, prompt_key)
        return self._cache.get(key)

    # ── Set ───────────────────────────────────────────────────────────────────

    def set(self, agent_id: str, prompt_key: str, content: str) -> None:
        """Stores a prompt in the cache."""
        key = self.build_key(agent_id, prompt_key)
        self._cache[key] = content
        logger.debug(f"Prompt cached: {key}")

    # ── Invalidate ────────────────────────────────────────────────────────────

    def invalidate(self, agent_id: str, prompt_key: str) -> None:
        """
        Removes a single prompt from cache.
        Called when Redis pub/sub receives a prompt_invalidated event.
        Next get() will trigger MongoDB lookup and re-cache.
        """
        key = self.build_key(agent_id, prompt_key)
        removed = self._cache.pop(key, None)
        if removed:
            logger.info(f"Prompt cache invalidated: {key}")

    def invalidate_all(self) -> None:
        """Clears entire cache. Used for emergency refresh or testing."""
        count = len(self._cache)
        self._cache.clear()
        logger.info(f"Prompt cache cleared — {count} entries removed")

    # ── Bulk load ─────────────────────────────────────────────────────────────

    def load_many(self, prompts: list[dict[str, Any]]) -> None:
        """
        Bulk loads prompts from MongoDB on startup.
        Each dict must have: agent_id, prompt_key, content

        Called once by PromptService.load_all_into_cache() during lifespan startup.
        """
        for prompt in prompts:
            self.set(
                agent_id=prompt["agent_id"],
                prompt_key=prompt["prompt_key"],
                content=prompt["content"]
            )
        logger.info(f"Prompt cache loaded — {len(prompts)} prompts loaded from MongoDB")

    # ── Debug ─────────────────────────────────────────────────────────────────

    def list_keys(self) -> list[str]:
        """Returns all cached keys — useful for health check / debug endpoint."""
        return list(self._cache.keys())

    def size(self) -> int:
        """Returns number of cached prompts."""
        return len(self._cache)


# ── Singleton ─────────────────────────────────────────────────────────────────
# Single instance — imported directly wherever prompt lookup is needed.
prompt_cache = PromptCache()