# app/db.py
# Centralised database clients — AsyncMongoClient + Redis.
# Created ONCE at FastAPI lifespan startup. Never instantiate inside handlers.
#
# Usage in main.py lifespan:
#   await db.connect()
#   yield
#   await db.disconnect()

import logging

import redis.asyncio as redis
from pymongo.asynchronous.mongo_client import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.server_api import ServerApi

from app.config import settings

logger = logging.getLogger(__name__)


class DatabaseClients:
    """Holds shared instances of AsyncMongoClient and Redis."""

    def __init__(self):
        self._mongo_client: AsyncMongoClient | None = None
        self._redis_client: redis.Redis | None = None

    async def connect(self) -> None:
        """Called once at FastAPI startup via lifespan."""
        await self._connect_mongo()
        await self._connect_redis()

    async def _connect_mongo(self) -> None:
        self._mongo_client = AsyncMongoClient(
            settings.MONGODB_URI,
            server_api=ServerApi("1"),
            maxPoolSize=20,
            minPoolSize=2,
        )
        await self._mongo_client.admin.command("ping")
        logger.info("MongoDB connected successfully")

    async def _connect_redis(self) -> None:
        self._redis_client = redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            max_connections=10,
        )
        await self._redis_client.ping()
        logger.info("Redis connected successfully")

    async def disconnect(self) -> None:
        """Called once at FastAPI shutdown via lifespan."""
        if self._mongo_client:
            self._mongo_client.close()
            logger.info("MongoDB disconnected")
        if self._redis_client:
            await self._redis_client.aclose()
            logger.info("Redis disconnected")

    @property
    def mongo_db(self) -> AsyncDatabase:
        if not self._mongo_client:
            raise RuntimeError("MongoDB client not initialised. Call connect() first.")
        return self._mongo_client[settings.MONGODB_DB_NAME]

    @property
    def redis(self) -> redis.Redis:
        if not self._redis_client:
            raise RuntimeError("Redis client not initialised. Call connect() first.")
        return self._redis_client

    @property
    def mongo_client(self) -> AsyncMongoClient:
        if not self._mongo_client:
            raise RuntimeError("MongoDB client not initialised. Call connect() first.")
        return self._mongo_client


# Singleton
db = DatabaseClients()


# FastAPI Dependency helpers
def get_mongo_db() -> AsyncDatabase:
    return db.mongo_db


def get_redis() -> redis.Redis:
    return db.redis