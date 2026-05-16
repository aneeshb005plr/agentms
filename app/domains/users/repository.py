# app/domains/users/repository.py
# MongoDB CRUD for users collection. No business logic here.
#
# Collection: `users`
# Primary key: user_id (string from JWT — not MongoDB ObjectId)
# Indexes:
#   user_id  — unique
#   email    — for lookup

import logging
from datetime import datetime, timezone

from pymongo.asynchronous.database import AsyncDatabase

from app.domains.users.schemas import UserCreate, UserResponse, UserUpdate

logger = logging.getLogger(__name__)

COLLECTION = "users"


class UserRepository:

    def __init__(self, db: AsyncDatabase):
        self._col = db[COLLECTION]

    async def setup_indexes(self) -> None:
        await self._col.create_index("user_id", unique=True, name="idx_user_id")
        await self._col.create_index("email", name="idx_email")
        logger.info("Users collection indexes created")

    async def find_by_id(self, user_id: str) -> dict | None:
        return await self._col.find_one({"user_id": user_id})

    async def create(self, data: UserCreate) -> dict:
        now = datetime.now(timezone.utc)
        doc = {
            "user_id":     data.user_id,
            "email":       data.email,
            "name":        data.name,
            "roles":       data.roles,
            "department":  None,
            "preferences": None,
            "first_seen":  now,
            "last_seen":   now,
            "is_active":   True,
        }
        await self._col.insert_one(doc)
        logger.info("New user created: %s", data.user_id)
        return doc

    async def update_last_seen(self, user_id: str) -> None:
        await self._col.update_one(
            {"user_id": user_id},
            {"$set": {"last_seen": datetime.now(timezone.utc)}},
        )

    async def update_profile(self, user_id: str, data: UserUpdate) -> dict | None:
        updates = {k: v for k, v in data.model_dump().items() if v is not None}
        if not updates:
            return await self.find_by_id(user_id)
        return await self._col.find_one_and_update(
            {"user_id": user_id},
            {"$set": updates},
            return_document=True,
        )

    async def deactivate(self, user_id: str) -> None:
        await self._col.update_one(
            {"user_id": user_id},
            {"$set": {"is_active": False}},
        )
        logger.info("User deactivated: %s", user_id)

    def to_response(self, doc: dict) -> UserResponse:
        return UserResponse(
            user_id=doc["user_id"],
            email=doc["email"],
            name=doc["name"],
            roles=doc.get("roles", []),
            department=doc.get("department"),
            preferences=doc.get("preferences"),
            first_seen=doc["first_seen"],
            last_seen=doc["last_seen"],
            is_active=doc.get("is_active", True),
        )