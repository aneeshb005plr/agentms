# app/dependencies.py
# Shared FastAPI dependency injection helpers.
# Import these in any router that needs DB, Redis, or services.
#
# Pattern — every dependency is a typed function returning the resource:
#   async def my_endpoint(db: MongoDB): ...
#
# FastAPI caches dependency results within the same request automatically.

from typing import Annotated

import redis.asyncio as redis
from fastapi import Depends
from pymongo.asynchronous.database import AsyncDatabase

from app.db import get_mongo_db, get_redis
from app.domains.auth.dependencies import CurrentUser  # re-export for convenience
from app.domains.prompts.service import PromptService
from app.domains.users.service import UserService


# ── DB type aliases ───────────────────────────────────────────────────────────
MongoDB = Annotated[AsyncDatabase, Depends(get_mongo_db)]
Redis   = Annotated[redis.Redis,   Depends(get_redis)]


# ── Service factories ─────────────────────────────────────────────────────────

def get_prompt_service(db: MongoDB, redis: Redis) -> PromptService:
    return PromptService(db=db, redis_client=redis)


def get_user_service(db: MongoDB) -> UserService:
    return UserService(db=db)


# ── Annotated type aliases — cleanest router signatures ───────────────────────
PromptSvc = Annotated[PromptService, Depends(get_prompt_service)]
UserSvc   = Annotated[UserService,   Depends(get_user_service)]

# Re-export CurrentUser for routers that only need auth
__all__ = [
    "MongoDB", "Redis",
    "PromptSvc", "UserSvc",
    "CurrentUser",
    "get_prompt_service", "get_user_service",
]