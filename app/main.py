# app/main.py
# Application factory — create_app() builds and returns the FastAPI instance.
#
# Why factory pattern?
#   - Each test gets a fresh app instance — no shared state between tests
#   - Easy to swap config for different environments
#   - Clean separation of app construction from app running
#
# Lifespan startup order:
#   1. Connect MongoDB + Redis
#   2. Initialise AuthService (loads JWKS client)
#   3. Setup MongoDB indexes for all collections
#   4. Seed default prompts if first boot
#   5. Load prompts into in-memory cache
#   6. Start Redis pub/sub invalidation listener

import asyncio
import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db import db
from app.api.v1.router import v1_router
from app.exceptions import register_exception_handlers
from app.middleware import register_middleware
from app.domains.auth.service import auth_service
from app.domains.prompts.service import PromptService
from app.domains.users.repository import UserRepository

logger = logging.getLogger(__name__)

_background_tasks: list[asyncio.Task] = []


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Manages the full application lifecycle.
    Everything before yield = startup.
    Everything after yield = shutdown.
    """

    # ── Startup ───────────────────────────────────────────────────────────────
    logger.info(
        f"Starting {settings.SERVICE_NAME} v{settings.SERVICE_VERSION} "
        f"[{settings.ENVIRONMENT}]"
    )

    # 1. Connect MongoDB and Redis
    await db.connect()

    # 2. Initialise AuthService (creates PyJWKClient singleton)
    auth_service.initialise()

    # 3. Setup MongoDB indexes
    user_repo = UserRepository(db.mongo_db)
    await user_repo.setup_indexes()

    prompt_service = PromptService(db=db.mongo_db, redis_client=db.redis)
    await prompt_service.setup_indexes()

    # 4. Seed default prompts if first boot
    await prompt_service.seed_default_prompts(seeded_by="system")

    # 5. Load all active prompts into in-memory cache
    await prompt_service.load_all_into_cache()

    # 6. Start Redis pub/sub invalidation listener as background task
    listener_task = asyncio.create_task(
        prompt_service.start_invalidation_listener(),
        name="prompt_invalidation_listener"
    )
    _background_tasks.append(listener_task)

    logger.info(f"{settings.SERVICE_NAME} startup complete")

    # ── Running ───────────────────────────────────────────────────────────────
    yield

    # ── Shutdown ──────────────────────────────────────────────────────────────
    logger.info(f"Shutting down {settings.SERVICE_NAME}...")

    for task in _background_tasks:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    _background_tasks.clear()
    await db.disconnect()

    logger.info(f"{settings.SERVICE_NAME} shutdown complete")


def create_app() -> FastAPI:
    """
    Application factory — builds and returns the configured FastAPI instance.
    Called by uvicorn in root main.py.
    Also called in tests to get a fresh app per test run.
    """
    app = FastAPI(
        title       = settings.SERVICE_NAME,
        version     = settings.SERVICE_VERSION,
        description = "NextGenAMS AI Agent Engine — PwC IT Support Automation",
        lifespan    = lifespan,
        docs_url    = "/docs"        if settings.ENVIRONMENT != "production" else None,
        redoc_url   = "/redoc"       if settings.ENVIRONMENT != "production" else None,
        openapi_url = "/openapi.json" if settings.ENVIRONMENT != "production" else None,
    )

    # ── CORS ──────────────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins     = settings.CORS_ORIGINS,
        allow_credentials = True,
        allow_methods     = ["*"],
        allow_headers     = ["*"],
    )

    # ── Custom middleware ─────────────────────────────────────────────────────
    register_middleware(app)

    # ── Exception handlers ────────────────────────────────────────────────────
    register_exception_handlers(app)

    # ── Routers ───────────────────────────────────────────────────────────────
    app.include_router(v1_router)

    return app