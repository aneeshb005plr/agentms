# app/api/v1/router.py
# Aggregates all v1 API routers into one.
# Registered in create_app() factory under /api/v1 prefix.

from fastapi import APIRouter

from app.api.v1.health import router as health_router
from app.api.v1.auth import router as auth_router

# Phase 1 — Week 2
# from app.api.v1.chat import router as chat_router

# Phase 2
# from app.api.v1.prompts import router as prompts_router
# from app.api.v1.users import router as users_router

v1_router = APIRouter(prefix="/api/v1")

v1_router.include_router(health_router)
v1_router.include_router(auth_router)

# v1_router.include_router(chat_router)       # Phase 1 Week 2
# v1_router.include_router(prompts_router)    # Phase 2
# v1_router.include_router(users_router)      # Phase 2