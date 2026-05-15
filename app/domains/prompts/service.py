# services/prompt_service.py
# Manages agent prompts — MongoDB as primary store, Redis for cache invalidation.
#
# Responsibilities:
#   1. get_prompt()        — full fallback chain: cache → MongoDB → config default
#   2. upsert_prompt()     — save/update prompt in MongoDB + publish Redis invalidation
#   3. load_all_into_cache() — bulk load all active prompts at startup
#   4. start_invalidation_listener() — background task subscribing to Redis pub/sub
#
# Collection: `prompts`
# Schema:
#   agent_id    str       e.g. "conversational_support_agent"
#   prompt_key  str       e.g. "system_prompt"
#   content     str       the actual prompt text
#   version     int       auto-incremented on every update
#   is_active   bool      True = currently in use (only one active per agent+key)
#   updated_by  str       user_id from JWT (admin who made the change)
#   updated_at  datetime
#   created_at  datetime

import asyncio
import json
import logging
from datetime import datetime, timezone

import redis.asyncio as redis
from pymongo.asynchronous.database import AsyncDatabase
from pymongo import DESCENDING

from app.config import settings
from app.domains.prompts.cache import PromptCache, prompt_cache

logger = logging.getLogger(__name__)

# ── Redis pub/sub channel ─────────────────────────────────────────────────────
# Message payload published when admin updates a prompt:
# { "agent_id": "conversational_support_agent", "prompt_key": "system_prompt" }
PROMPT_CHANNEL = settings.REDIS_PROMPT_CHANNEL


class PromptService:
    """
    Handles all prompt operations — MongoDB persistence + cache management.
    Instantiated once and shared via FastAPI dependency injection.
    """

    def __init__(self, db: AsyncDatabase, redis_client: redis.Redis):
        self._db           = db
        self._redis        = redis_client
        self._collection   = db[settings.MONGODB_PROMPTS_COLLECTION]
        self._cache        = prompt_cache   # singleton reference

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
            partialFilterExpression={"is_active": True},  # unique only for active docs
            name="idx_active_prompt_per_agent_key"
        )
        await self._collection.create_index(
            [("agent_id", 1), ("prompt_key", 1)],
            name="idx_agent_prompt_lookup"
        )
        logger.info("Prompt collection indexes created")

    async def load_all_into_cache(self) -> None:
        """
        Bulk loads all active prompts from MongoDB into in-memory cache.
        Called once at FastAPI startup after MongoDB connects.
        If MongoDB is empty (first boot), cache stays empty — fallback to config defaults.
        """
        cursor = self._collection.find({"is_active": True})
        prompts = []
        async for doc in cursor:
            prompts.append({
                "agent_id":   doc["agent_id"],
                "prompt_key": doc["prompt_key"],
                "content":    doc["content"]
            })

        if prompts:
            self._cache.load_many(prompts)
        else:
            logger.info(
                "No prompts found in MongoDB — using config.py defaults. "
                "Run seed_default_prompts() to populate."
            )

    # ── Get prompt (full fallback chain) ──────────────────────────────────────

    async def get_prompt(self, agent_id: str, prompt_key: str) -> str:
        """
        Returns prompt content via fallback chain:
          1. In-memory cache  (fastest — microseconds)
          2. MongoDB          (on cache miss — milliseconds)
          3. config.py _DEFAULT (fallback if MongoDB has no entry)

        This is called by agents on every invocation — must be fast.
        """
        # ── 1. Cache hit ──────────────────────────────────────────────────────
        cached = self._cache.get(agent_id, prompt_key)
        if cached:
            return cached

        # ── 2. MongoDB lookup ─────────────────────────────────────────────────
        doc = await self._collection.find_one({
            "agent_id":   agent_id,
            "prompt_key": prompt_key,
            "is_active":  True
        })

        if doc:
            content = doc["content"]
            self._cache.set(agent_id, prompt_key, content)   # re-populate cache
            logger.debug(f"Prompt loaded from MongoDB and cached: {agent_id}:{prompt_key}")
            return content

        # ── 3. Config default fallback ────────────────────────────────────────
        default = self._get_default(agent_id, prompt_key)
        logger.warning(
            f"Prompt not found in MongoDB — using config default: {agent_id}:{prompt_key}"
        )
        return default

    # ── Upsert (Admin UI will call this in Phase 2) ───────────────────────────

    async def upsert_prompt(
        self,
        agent_id:   str,
        prompt_key: str,
        content:    str,
        updated_by: str   # user_id of the admin making the change
    ) -> dict:
        """
        Saves or updates a prompt in MongoDB.
        Deactivates previous version, inserts new active version.
        Publishes Redis invalidation so all pods refresh their cache.

        Returns the saved document.
        """
        now = datetime.now(timezone.utc)

        # Deactivate current active version
        await self._collection.update_many(
            {"agent_id": agent_id, "prompt_key": prompt_key, "is_active": True},
            {"$set": {"is_active": False}}
        )

        # Get next version number
        last = await self._collection.find_one(
            {"agent_id": agent_id, "prompt_key": prompt_key},
            sort=[("version", DESCENDING)]
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
            "created_at": now if not last else last.get("created_at", now)
        }
        result = await self._collection.insert_one(new_doc)
        new_doc["_id"] = str(result.inserted_id)

        logger.info(
            f"Prompt saved: {agent_id}:{prompt_key} "
            f"v{next_version} by {updated_by}"
        )

        # Publish Redis invalidation — all pods will invalidate + reload
        await self._publish_invalidation(agent_id, prompt_key)

        return new_doc

    # ── Prompt history (Admin UI) ─────────────────────────────────────────────

    async def get_prompt_history(
        self,
        agent_id:   str,
        prompt_key: str,
        limit:      int = 10
    ) -> list[dict]:
        """
        Returns version history for a prompt — used by Admin UI.
        Ordered newest first.
        """
        cursor = self._collection.find(
            {"agent_id": agent_id, "prompt_key": prompt_key},
            sort=[("version", DESCENDING)],
            limit=limit
        )
        history = []
        async for doc in cursor:
            doc["_id"] = str(doc["_id"])
            history.append(doc)
        return history

    # ── Seed defaults (first boot utility) ───────────────────────────────────

    async def seed_default_prompts(self, seeded_by: str = "system") -> None:
        """
        Seeds MongoDB with config.py defaults if prompts collection is empty.
        Safe to call multiple times — skips existing prompts.
        Called once on first boot or after database reset.
        """
        defaults = [
            (
                PromptCache.CONVERSATIONAL_SUPPORT_AGENT,
                PromptCache.SYSTEM_PROMPT,
                settings.CONVERSATIONAL_SUPPORT_AGENT_SYSTEM_PROMPT_DEFAULT
            ),
            (
                PromptCache.CONVERSATIONAL_SUPPORT_AGENT,
                PromptCache.QUERY_REWRITE,
                settings.QUERY_REWRITE_PROMPT_DEFAULT
            ),
            (
                PromptCache.TITLE_GENERATION,
                PromptCache.TITLE_PROMPT,
                settings.TITLE_GENERATION_PROMPT_DEFAULT
            ),
        ]

        seeded = 0
        for agent_id, prompt_key, content in defaults:
            exists = await self._collection.find_one({
                "agent_id":  agent_id,
                "prompt_key": prompt_key,
                "is_active":  True
            })
            if not exists:
                await self.upsert_prompt(
                    agent_id=agent_id,
                    prompt_key=prompt_key,
                    content=content,
                    updated_by=seeded_by
                )
                seeded += 1

        if seeded:
            logger.info(f"Seeded {seeded} default prompts into MongoDB")
        else:
            logger.info("Default prompts already exist in MongoDB — skipping seed")

    # ── Redis pub/sub — Publisher ─────────────────────────────────────────────

    async def _publish_invalidation(self, agent_id: str, prompt_key: str) -> None:
        """
        Publishes cache invalidation event to Redis channel.
        All subscribed pods will invalidate their local cache for this prompt.
        """
        payload = json.dumps({"agent_id": agent_id, "prompt_key": prompt_key})
        await self._redis.publish(PROMPT_CHANNEL, payload)
        logger.debug(f"Cache invalidation published: {agent_id}:{prompt_key}")

    # ── Redis pub/sub — Subscriber (background task) ──────────────────────────

    async def start_invalidation_listener(self) -> None:
        """
        Long-running background task — listens for cache invalidation events.
        Started once in main.py lifespan as an asyncio background task.
        When a message arrives, invalidates the local in-memory cache for that prompt.
        Next request will reload from MongoDB automatically.
        """
        logger.info(f"Prompt cache invalidation listener started on: {PROMPT_CHANNEL}")

        while True:
            try:
                # Create a fresh pubsub connection for the subscriber
                # (separate from the main redis client used for publishing)
                async with self._redis.pubsub() as pubsub:
                    await pubsub.subscribe(PROMPT_CHANNEL)

                    async for message in pubsub.listen():
                        if message["type"] != "message":
                            continue

                        try:
                            data = json.loads(message["data"])
                            agent_id   = data.get("agent_id")
                            prompt_key = data.get("prompt_key")

                            if agent_id and prompt_key:
                                self._cache.invalidate(agent_id, prompt_key)
                                logger.info(
                                    f"Cache invalidated via Redis: {agent_id}:{prompt_key}"
                                )

                        except (json.JSONDecodeError, KeyError) as e:
                            logger.warning(f"Invalid invalidation message received: {e}")

            except Exception as e:
                # Reconnect on Redis connection loss — retry after 5 seconds
                logger.error(f"Invalidation listener error: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)

    # ── Config default lookup ─────────────────────────────────────────────────

    @staticmethod
    def _get_default(agent_id: str, prompt_key: str) -> str:
        """
        Maps agent_id + prompt_key to config.py _DEFAULT values.
        Last resort fallback — should only trigger on first boot.
        """
        defaults = {
            f"{PromptCache.CONVERSATIONAL_SUPPORT_AGENT}:{PromptCache.SYSTEM_PROMPT}":
                settings.CONVERSATIONAL_SUPPORT_AGENT_SYSTEM_PROMPT_DEFAULT,

            f"{PromptCache.CONVERSATIONAL_SUPPORT_AGENT}:{PromptCache.QUERY_REWRITE}":
                settings.QUERY_REWRITE_PROMPT_DEFAULT,

            f"{PromptCache.TITLE_GENERATION}:{PromptCache.TITLE_PROMPT}":
                settings.TITLE_GENERATION_PROMPT_DEFAULT,
        }

        key = f"{agent_id}:{prompt_key}"
        default = defaults.get(key)

        if not default:
            raise ValueError(
                f"No default prompt found for: {key}. "
                f"Check config.py and PromptCache constants."
            )

        return default