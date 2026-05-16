# app/main.py
# Application factory — create_app() builds and returns FastAPI instance.
# All logic lives here. Root main.py is just the uvicorn entry point.

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import v1_router
from app.config import settings
from app.db import db
from app.domains.auth.service import auth_service
from app.domains.prompts.service import PromptService
from app.domains.users.repository import UserRepository
from app.exceptions import register_exception_handlers
from app.middleware import register_middleware

logger = logging.getLogger(__name__)

_background_tasks: list[asyncio.Task] = []


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:

    # ── Startup ───────────────────────────────────────────────────────────────
    logger.info(
        "Starting %s v%s [%s]",
        settings.SERVICE_NAME, settings.SERVICE_VERSION, settings.ENVIRONMENT,
    )
    logger.info("Settings: %s", settings.safe_dump())

    # 1. Connect MongoDB + Redis
    await db.connect()

    # 2. Initialise AuthService (creates PyJWKClient singleton)
    auth_service.initialise()

    # 3. Setup MongoDB indexes
    await UserRepository(db.mongo_db).setup_indexes()

    prompt_service = PromptService(db=db.mongo_db, redis_client=db.redis)
    await prompt_service.setup_indexes()

    # 4. Seed default prompts on first boot
    await prompt_service.seed_default_prompts(seeded_by="system")

    # 5. Load all active prompts into in-memory cache
    await prompt_service.load_all_into_cache()

    # 6. Start Redis pub/sub invalidation listener
    task = asyncio.create_task(
        prompt_service.start_invalidation_listener(),
        name="prompt_invalidation_listener",
    )
    _background_tasks.append(task)

    logger.info("%s startup complete", settings.SERVICE_NAME)

    # ── Running ───────────────────────────────────────────────────────────────
    yield

    # ── Shutdown ──────────────────────────────────────────────────────────────
    logger.info("Shutting down %s...", settings.SERVICE_NAME)

    for task in _background_tasks:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    _background_tasks.clear()
    await db.disconnect()

    logger.info("%s shutdown complete", settings.SERVICE_NAME)


def create_app() -> FastAPI:
    """
    Application factory.
    Called by uvicorn entry point (root main.py).
    Also called in tests — each test gets a fresh app instance.
    """
    app = FastAPI(
        title       = settings.SERVICE_NAME,
        version     = settings.SERVICE_VERSION,
        description = settings.SERVICE_DESCRIPTION,
        lifespan    = lifespan,
        docs_url    = "/docs"         if settings.ENABLE_SWAGGER and not settings.IS_PRODUCTION else None,
        redoc_url   = "/redoc"        if settings.ENABLE_SWAGGER and not settings.IS_PRODUCTION else None,
        openapi_url = "/openapi.json" if settings.ENABLE_SWAGGER and not settings.IS_PRODUCTION else None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins     = settings.CORS_ORIGINS,
        allow_credentials = True,
        allow_methods     = ["*"],
        allow_headers     = ["*"],
    )

    register_middleware(app)
    register_exception_handlers(app)
    app.include_router(v1_router)

    return app