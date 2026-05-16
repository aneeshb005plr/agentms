# app/domains/prompts/service.py
# Manages agent prompts — MongoDB as primary store, Redis for cache invalidation.
#
# Responsibilities:
#   1. get_prompt()                  — full fallback chain: cache → MongoDB → defaults module
#   2. upsert_prompt()               — save/update prompt in MongoDB + publish Redis invalidation
#   3. load_all_into_cache()         — bulk load all active prompts at startup
#   4. start_invalidation_listener() — background task subscribing to Redis pub/sub
#
# Collection: `prompts`
# Schema:
#   agent_id     str       e.g. "conversational_support_agent"
#   prompt_key   str       e.g. "system_prompt"
#   content      str       the actual prompt text
#   version      int       auto-incremented on every update
#   is_active    bool      True = currently in use (only one active per agent+key)
#   updated_by   str       user_id from JWT (admin who made the change)
#   updated_at   datetime
#   created_at   datetime

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import List

import redis.asyncio as redis
from pymongo import DESCENDING
from pymongo.asynchronous.database import AsyncDatabase

from app.config import settings
from app.domains.prompts import defaults as prompt_defaults
from app.domains.prompts.cache import PromptCache, prompt_cache

logger = logging.getLogger(__name__)

PROMPT_CHANNEL = settings.REDIS_PROMPT_CHANNEL

# Default prompt mapping — tuple key (agent_id, prompt_key) → content
_DEFAULT_PROMPTS: dict[tuple[str, str], str] = {
    (
        PromptCache.CONVERSATIONAL_SUPPORT_AGENT,
        PromptCache.SYSTEM_PROMPT,
    ): prompt_defaults.CONVERSATIONAL_SUPPORT_AGENT_SYSTEM_PROMPT,

    (
        PromptCache.CONVERSATIONAL_SUPPORT_AGENT,
        PromptCache.QUERY_REWRITE,
    ): prompt_defaults.QUERY_REWRITE_PROMPT,

    (
        PromptCache.TITLE_GENERATION,
        PromptCache.TITLE_PROMPT,
    ): prompt_defaults.TITLE_GENERATION_PROMPT,

    (
        PromptCache.CONVERSATIONAL_SUPPORT_AGENT,
        PromptCache.CONVERSATION_SUMMARY,
    ): prompt_defaults.CONVERSATION_SUMMARY_PROMPT,
}


class PromptService:
    """
    Handles all prompt operations — MongoDB persistence + cache management.
    Instantiated per request via FastAPI dependency injection.
    """

    def __init__(self, db: AsyncDatabase, redis_client: redis.Redis):
        self._db         = db
        self._redis      = redis_client
        self._collection = db[settings.MONGODB_PROMPTS_COLLECTION]
        self._cache      = prompt_cache  # singleton reference

    # ── Startup ───────────────────────────────────────────────────────────────

    async def setup_indexes(self) -> None:
        """
        Creates MongoDB indexes on startup.
        Compound unique index ensures one active prompt per agent+key.
        Called once in main.py lifespan.
        """
        await self._collection.create_index(
            [("agent_id", 1), ("prompt_key", 1), ("is_active", 1)],
            unique=True,
            partialFilterExpression={"is_active": True},
            name="idx_active_prompt_per_agent_key",
        )
        await self._collection.create_index(
            [("agent_id", 1), ("prompt_key", 1)],
            name="idx_agent_prompt_lookup",
        )
        logger.info("Prompt collection indexes created")

    async def load_all_into_cache(self) -> None:
        """
        Bulk loads all active prompts from MongoDB into in-memory cache.
        Called once at FastAPI startup. If MongoDB empty → fallback to defaults.
        """
        cursor = self._collection.find({"is_active": True})
        prompts = []

        async for doc in cursor:
            prompts.append({
                "agent_id":   doc["agent_id"],
                "prompt_key": doc["prompt_key"],
                "content":    doc["content"],
            })

        if prompts:
            self._cache.load_many(prompts)
        else:
            logger.info(
                "No prompts found in MongoDB — using defaults module. "
                "Run seed_default_prompts() to populate."
            )

    # ── Get prompt (full fallback chain) ──────────────────────────────────────

    async def get_prompt(self, agent_id: str, prompt_key: str) -> str:
        """
        Returns prompt content via fallback chain:
          1. In-memory cache  (fastest — microseconds)
          2. MongoDB          (cache miss — milliseconds)
          3. defaults module  (last resort — first boot only)
        """
        # 1. Cache hit
        cached = self._cache.get(agent_id, prompt_key)
        if cached:
            return cached

        # 2. MongoDB lookup
        doc = await self._collection.find_one({
            "agent_id":   agent_id,
            "prompt_key": prompt_key,
            "is_active":  True,
        })

        if doc:
            content = doc["content"]
            self._cache.set(agent_id, prompt_key, content)
            logger.debug("Prompt loaded from MongoDB and cached: %s:%s", agent_id, prompt_key)
            return content

        # 3. Defaults fallback
        default = self._get_default(agent_id, prompt_key)
        logger.warning("Prompt not in MongoDB — using default: %s:%s", agent_id, prompt_key)
        return default

    # ── Upsert ────────────────────────────────────────────────────────────────

    async def upsert_prompt(
        self,
        agent_id:   str,
        prompt_key: str,
        content:    str,
        updated_by: str,
    ) -> dict:
        """
        Saves or updates a prompt in MongoDB.
        Deactivates previous version, inserts new active version.
        Publishes Redis invalidation so all AKS pods refresh cache.
        Returns saved document.
        """
        now = datetime.now(timezone.utc)

        # Deactivate current active version
        await self._collection.update_many(
            {"agent_id": agent_id, "prompt_key": prompt_key, "is_active": True},
            {"$set": {"is_active": False}},
        )

        # Get next version number
        last = await self._collection.find_one(
            {"agent_id": agent_id, "prompt_key": prompt_key},
            sort=[("version", DESCENDING)],
        )
        next_version = (last["version"] + 1) if last else 1

        # Insert new active version
        new_doc = {
            "agent_id":   agent_id,
            "prompt_key": prompt_key,
            "content":    content,
            "version":    next_version,
            "is_active":  True,
            "updated_by": updated_by,
            "updated_at": now,
            "created_at": now if not last else last.get("created_at", now),
        }

        result = await self._collection.insert_one(new_doc)
        new_doc["_id"] = str(result.inserted_id)

        logger.info(
            "Prompt saved: %s:%s v%d by %s",
            agent_id, prompt_key, next_version, updated_by,
        )

        await self._publish_invalidation(agent_id, prompt_key)
        return new_doc

    # ── Prompt history ────────────────────────────────────────────────────────

    async def get_prompt_history(
        self,
        agent_id:   str,
        prompt_key: str,
        limit:      int = 10,
    ) -> List[dict]:
        """Returns version history — newest first. Used by Admin UI (Phase 2)."""
        cursor = self._collection.find(
            {"agent_id": agent_id, "prompt_key": prompt_key},
            sort=[("version", DESCENDING)],
            limit=limit,
        )
        history = []
        async for doc in cursor:
            doc["_id"] = str(doc["_id"])
            history.append(doc)
        return history

    # ── Seed defaults ─────────────────────────────────────────────────────────

    async def seed_default_prompts(self, seeded_by: str = "system") -> None:
        """
        Seeds MongoDB with defaults from defaults.py if collection is empty.
        Safe to call multiple times — skips existing prompts.
        Called once on first boot in lifespan.
        """
        seeded = 0

        for (agent_id, prompt_key), content in _DEFAULT_PROMPTS.items():
            exists = await self._collection.find_one({
                "agent_id":   agent_id,
                "prompt_key": prompt_key,
                "is_active":  True,
            })
            if not exists:
                await self.upsert_prompt(
                    agent_id=agent_id,
                    prompt_key=prompt_key,
                    content=content,
                    updated_by=seeded_by,
                )
                seeded += 1

        if seeded:
            logger.info("Seeded %d default prompts into MongoDB", seeded)
        else:
            logger.info("Default prompts already exist — skipping seed")

    # ── Redis publisher ───────────────────────────────────────────────────────

    async def _publish_invalidation(self, agent_id: str, prompt_key: str) -> None:
        payload = json.dumps({"agent_id": agent_id, "prompt_key": prompt_key})
        await self._redis.publish(PROMPT_CHANNEL, payload)
        logger.debug("Cache invalidation published: %s:%s", agent_id, prompt_key)

    # ── Redis subscriber (background task) ───────────────────────────────────

    async def start_invalidation_listener(self) -> None:
        """
        Long-running background task — listens for cache invalidation events.
        Started once in lifespan. Auto-reconnects on Redis connection loss.
        Handles CancelledError cleanly for graceful shutdown.
        """
        logger.info("Prompt cache invalidation listener started on: %s", PROMPT_CHANNEL)

        while True:
            try:
                async with self._redis.pubsub() as pubsub:
                    await pubsub.subscribe(PROMPT_CHANNEL)

                    async for message in pubsub.listen():
                        if message["type"] != "message":
                            continue
                        try:
                            data      = json.loads(message["data"])
                            agent_id  = data.get("agent_id")
                            prompt_key = data.get("prompt_key")
                            if agent_id and prompt_key:
                                self._cache.invalidate(agent_id, prompt_key)
                                logger.info(
                                    "Cache invalidated via Redis: %s:%s",
                                    agent_id, prompt_key,
                                )
                        except (json.JSONDecodeError, KeyError) as e:
                            logger.warning("Invalid invalidation message: %s", e)

            except asyncio.CancelledError:
                logger.info("Prompt cache invalidation listener stopping")
                raise  # re-raise so lifespan shutdown completes cleanly

            except Exception as e:
                logger.error("Invalidation listener error: %s. Reconnecting in 5s...", e)
                await asyncio.sleep(5)

    # ── Defaults lookup ───────────────────────────────────────────────────────

    @staticmethod
    def _get_default(agent_id: str, prompt_key: str) -> str:
        default = _DEFAULT_PROMPTS.get((agent_id, prompt_key))
        if not default:
            raise ValueError(
                f"No default prompt found for: {agent_id}:{prompt_key}. "
                f"Check app/domains/prompts/defaults.py and PromptCache constants."
            )
        return default