# app/dependencies.py
# Shared FastAPI dependency injection helpers.

from typing import Annotated

import redis.asyncio as redis
from fastapi import Depends
from pymongo.asynchronous.database import AsyncDatabase

from app.db import get_mongo_db, get_redis
from app.domains.auth.dependencies import CurrentUser  # re-export
from app.domains.prompts.service import PromptService
from app.domains.users.service import UserService
from app.domains.conversations.service import ConversationService

# ── Type aliases ──────────────────────────────────────────────────────────────
MongoDB = Annotated[AsyncDatabase, Depends(get_mongo_db)]
Redis   = Annotated[redis.Redis,   Depends(get_redis)]


# ── Service factories ─────────────────────────────────────────────────────────
def get_prompt_service(db: MongoDB, redis: Redis) -> PromptService:
    return PromptService(db=db, redis_client=redis)

def get_user_service(db: MongoDB) -> UserService:
    return UserService(db=db)

def get_conversation_service(db: MongoDB) -> ConversationService:
    return ConversationService(db=db)


# ── Annotated aliases — cleanest router signatures ────────────────────────────
PromptSvc = Annotated[PromptService, Depends(get_prompt_service)]
UserSvc           = Annotated[UserService,          Depends(get_user_service)]
ConversationSvc   = Annotated[ConversationService, Depends(get_conversation_service)]

__all__ = [
    "MongoDB", "Redis",
    "PromptSvc", "UserSvc", "ConversationSvc",
    "CurrentUser",
    "get_prompt_service", "get_user_service", "get_conversation_service",
]