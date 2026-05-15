# core/db.py
# Centralised database clients — AsyncMongoClient + Redis.
# Created ONCE at FastAPI lifespan startup. Never instantiate inside handlers.
#
# Usage in main.py lifespan:
#   await db.connect()
#   yield
#   await db.disconnect()
#
# Usage anywhere else via dependency injection:
#   from core.db import get_mongo_db, get_redis

import logging
import redis.asyncio as redis
from pymongo.asynchronous.mongo_client import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.server_api import ServerApi
from app.config import settings

logger = logging.getLogger(__name__)


class DatabaseClients:
    """
    Holds single shared instances of AsyncMongoClient and Redis.
    Instantiated once at module level — lifespan calls connect() / disconnect().
    """

    def __init__(self):
        self._mongo_client: AsyncMongoClient | None = None
        self._redis_client: redis.Redis | None = None

    # ── Connect ───────────────────────────────────────────────────────────────

    async def connect(self):
        """Called once at FastAPI startup via lifespan."""
        await self._connect_mongo()
        await self._connect_redis()

    async def _connect_mongo(self):
        self._mongo_client = AsyncMongoClient(
            settings.MONGODB_URI,
            server_api=ServerApi("1"),    # MongoDB stable API — required for Atlas
            maxPoolSize=20,               # tune based on pod count and load
            minPoolSize=2,
        )
        # Ping to verify connection at startup — fail fast if misconfigured
        await self._mongo_client.admin.command("ping")
        logger.info("MongoDB connected successfully")

    async def _connect_redis(self):
        self._redis_client = redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,        # return str not bytes — cleaner to work with
            max_connections=10,
        )
        await self._redis_client.ping()
        logger.info("Redis connected successfully")

    # ── Disconnect ────────────────────────────────────────────────────────────

    async def disconnect(self):
        """Called once at FastAPI shutdown via lifespan."""
        if self._mongo_client:
            self._mongo_client.close()
            logger.info("MongoDB disconnected")

        if self._redis_client:
            await self._redis_client.aclose()
            logger.info("Redis disconnected")

    # ── Accessors ─────────────────────────────────────────────────────────────

    @property
    def mongo_db(self) -> AsyncDatabase:
        """Returns the nextgenams database handle."""
        if not self._mongo_client:
            raise RuntimeError("MongoDB client not initialised. Call connect() first.")
        return self._mongo_client[settings.MONGODB_DB_NAME]

    @property
    def redis(self) -> redis.Redis:
        """Returns the shared Redis client."""
        if not self._redis_client:
            raise RuntimeError("Redis client not initialised. Call connect() first.")
        return self._redis_client

    @property
    def mongo_client(self) -> AsyncMongoClient:
        """
        Returns raw AsyncMongoClient.
        Needed by LangGraph MongoDBSaver checkpointer which requires MongoClient directly.
        Note: LangGraph checkpointer uses sync MongoClient separately — see master_graph.py
        """
        if not self._mongo_client:
            raise RuntimeError("MongoDB client not initialised. Call connect() first.")
        return self._mongo_client


# ── Singleton ─────────────────────────────────────────────────────────────────
# Single instance shared across the entire application.
# Imported and used via get_mongo_db() and get_redis() below.
db = DatabaseClients()


# ── FastAPI Dependency Injection helpers ──────────────────────────────────────

def get_mongo_db() -> AsyncDatabase:
    """
    FastAPI dependency — injects MongoDB database handle.

    Usage in any endpoint or service:
        from core.db import get_mongo_db
        from pymongo.asynchronous.database import AsyncDatabase

        async def my_endpoint(db: AsyncDatabase = Depends(get_mongo_db)):
            ...
    """
    return db.mongo_db


def get_redis() -> redis.Redis:
    """
    FastAPI dependency — injects Redis client.

    Usage in any endpoint or service:
        from core.db import get_redis
        import redis.asyncio as redis

        async def my_endpoint(redis: redis.Redis = Depends(get_redis)):
            ...
    """
    return db.redis