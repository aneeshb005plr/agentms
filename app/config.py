# app/domains/users/service.py
# User onboarding — called after every successful JWT validation.
# First login → create user. Repeat login → update last_seen only.

import logging

from pymongo.asynchronous.database import AsyncDatabase

from app.domains.auth.schemas import UserClaims
from app.domains.users.repository import UserRepository
from app.domains.users.schemas import UserCreate, UserResponse

logger = logging.getLogger(__name__)


class UserService:

    def __init__(self, db: AsyncDatabase):
        self._repo = UserRepository(db)

    async def upsert_on_login(self, claims: UserClaims) -> UserResponse:
        """
        Auto-onboards user on first login.
        Updates last_seen on every subsequent login.
        """
        existing = await self._repo.find_by_id(claims.user_id)

        if existing:
            await self._repo.update_last_seen(claims.user_id)
            return self._repo.to_response(existing)

        doc = await self._repo.create(UserCreate(
            user_id=claims.user_id,
            email=claims.email,
            name=claims.name,
            roles=claims.roles,
        ))
        logger.info("User onboarded: %s (%s)", claims.user_id, claims.email)
        return self._repo.to_response(doc)

    async def get_user(self, user_id: str) -> UserResponse | None:
        doc = await self._repo.find_by_id(user_id)
        return self._repo.to_response(doc) if doc else None