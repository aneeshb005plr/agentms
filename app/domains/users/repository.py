# app/domains/users/repository.py
# Data access layer for users collection.
# Only MongoDB queries live here — no business logic.
# Service layer calls repository — routers call service.
#
# Collection: `users`
# Primary key: user_id (string from JWT claim — not MongoDB ObjectId)

import logging
from datetime import datetime, timezone

from pymongo.asynchronous.database import AsyncDatabase

from app.domains.users.schemas import UserCreate, UserUpdate, UserResponse

logger = logging.getLogger(__name__)

COLLECTION = "users"


class UserRepository:
    """
    MongoDB CRUD for users collection.
    Injected into UserService via constructor — fully replaceable in tests.
    """

    def __init__(self, db: AsyncDatabase):
        self._col = db[COLLECTION]

    async def setup_indexes(self) -> None:
        """
        Creates indexes on startup.
        user_id is our primary key — must be unique.
        email indexed for lookup.
        """
        await self._col.create_index("user_id", unique=True, name="idx_user_id")
        await self._col.create_index("email", name="idx_email")
        logger.info("Users collection indexes created")

    async def find_by_id(self, user_id: str) -> dict | None:
        """Returns user document or None if not found."""
        return await self._col.find_one({"user_id": user_id})

    async def create(self, data: UserCreate) -> dict:
        """
        Inserts a new user document.
        Called on first login — JWT provides initial data.
        """
        now = datetime.now(timezone.utc)
        doc = {
            "user_id":     data.user_id,
            "email":       data.email,
            "name":        data.name,
            "roles":       data.roles,
            "department":  None,      # filled later by user or admin
            "preferences": None,      # filled later by user
            "first_seen":  now,
            "last_seen":   now,
            "is_active":   True,
        }
        await self._col.insert_one(doc)
        logger.info(f"New user created: {data.user_id}")
        return doc

    async def update_last_seen(self, user_id: str) -> None:
        """Updates last_seen timestamp on every login."""
        await self._col.update_one(
            {"user_id": user_id},
            {"$set": {"last_seen": datetime.now(timezone.utc)}}
        )

    async def update_profile(self, user_id: str, data: UserUpdate) -> dict | None:
        """
        Updates user-editable fields.
        Only updates fields that are explicitly provided (PATCH semantics).
        Returns updated document or None if user not found.
        """
        updates = {k: v for k, v in data.model_dump().items() if v is not None}
        if not updates:
            return await self.find_by_id(user_id)

        result = await self._col.find_one_and_update(
            {"user_id": user_id},
            {"$set": updates},
            return_document=True    # return updated document
        )
        return result

    async def deactivate(self, user_id: str) -> None:
        """Soft delete — sets is_active=False. Admin only (Phase 2)."""
        await self._col.update_one(
            {"user_id": user_id},
            {"$set": {"is_active": False}}
        )
        logger.info(f"User deactivated: {user_id}")

    def _to_response(self, doc: dict) -> UserResponse:
        """Maps MongoDB document to UserResponse schema."""
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