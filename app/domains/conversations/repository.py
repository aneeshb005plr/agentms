# app/domains/conversations/repository.py
# MongoDB CRUD for conversations, messages, and token_usage collections.
# Pure data access — no business logic here.
#
# Collections:
#   conversations  — session metadata + sidebar + pre-aggregated token summary
#   messages       — individual messages (separate — NOT embedded)
#   token_usage    — one doc per LLM call (granular audit trail)
#
# Token tracking design:
#   - save_token_usage()                  → inserts one doc per LLM call
#   - update_conversation_token_totals()  → atomic $inc on conversation doc
#     Uses MongoDB dot-notation: "token_usage.by_agent.{agent}.input_tokens"
#     $inc creates nested fields automatically — no pre-initialisation needed
#
# Indexes:
#   conversations: { user_id, last_message_at }
#                  { conversation_id } unique
#   messages:      { conversation_id, created_at }
#                  { message_id } unique
#   token_usage:   { conversation_id, timestamp }
#                  { user_id, timestamp }
#                  { model, timestamp }
#                  { agent, timestamp }

import logging
from datetime import datetime, timezone

from pymongo import ASCENDING, DESCENDING
from pymongo.asynchronous.database import AsyncDatabase

from app.domains.conversations.schemas import (
    ConversationCreate,
    ConversationResponse,
    ConversationTokenUsage,
    ConversationUpdate,
    LLMCallUsage,
    MessageCreate,
    MessageResponse,
    PaginatedMessages,
)

logger = logging.getLogger(__name__)

CONVERSATIONS_COLLECTION = "conversations"
MESSAGES_COLLECTION      = "messages"
TOKEN_USAGE_COLLECTION   = "token_usage"
PAGE_SIZE                = 30


class ConversationRepository:

    def __init__(self, db: AsyncDatabase):
        self._conv  = db[CONVERSATIONS_COLLECTION]
        self._msgs  = db[MESSAGES_COLLECTION]
        self._usage = db[TOKEN_USAGE_COLLECTION]

    # ── Indexes ───────────────────────────────────────────────────────────────

    async def setup_indexes(self) -> None:
        # conversations
        await self._conv.create_index(
            "conversation_id", unique=True, name="idx_conversation_id"
        )
        await self._conv.create_index(
            [("user_id", ASCENDING), ("last_message_at", DESCENDING)],
            name="idx_user_conversations",
        )

        # messages
        await self._msgs.create_index(
            "message_id", unique=True, name="idx_message_id"
        )
        await self._msgs.create_index(
            [("conversation_id", ASCENDING), ("created_at", ASCENDING)],
            name="idx_conversation_messages",
        )

        # token_usage — analytics and audit queries
        await self._usage.create_index(
            [("conversation_id", ASCENDING), ("timestamp", DESCENDING)],
            name="idx_usage_conversation",
        )
        await self._usage.create_index(
            [("user_id", ASCENDING), ("timestamp", DESCENDING)],
            name="idx_usage_user",
        )
        await self._usage.create_index(
            [("model", ASCENDING), ("timestamp", DESCENDING)],
            name="idx_usage_model",
        )
        await self._usage.create_index(
            [("agent", ASCENDING), ("timestamp", DESCENDING)],
            name="idx_usage_agent",
        )

        logger.info("Conversations, messages, token_usage indexes created")

    # ── Conversation CRUD ─────────────────────────────────────────────────────

    async def create_conversation(self, data: ConversationCreate) -> dict:
        now = datetime.now(timezone.utc)
        doc = {
            "conversation_id": data.conversation_id,
            "user_id":         data.user_id,
            "title":           data.title,
            "summary":         None,
            "last_message":    None,
            "last_message_at": None,
            "message_count":   0,
            # Pre-initialised token_usage — $inc works on existing fields
            "token_usage": {
                "total_input_tokens":  0,
                "total_output_tokens": 0,
                "total_tokens":        0,
                "by_agent":            {},
                "by_model":            {},
            },
            "created_at":  now,
            "is_deleted":  False,
        }
        await self._conv.insert_one(doc)
        logger.info("Conversation created: %s", data.conversation_id)
        return doc

    async def get_conversation(self, conversation_id: str) -> dict | None:
        return await self._conv.find_one({"conversation_id": conversation_id})

    async def get_user_conversations(
        self,
        user_id:    str,
        limit:      int = 20,
        before:     str | None = None,    # ISO datetime cursor for pagination
    ) -> dict:
        """
        Returns paginated conversation list for sidebar — newest first.
        Uses cursor-based pagination via last_message_at timestamp.
        Returns: { conversations: list, has_more: bool }
        """
        query: dict = {"user_id": user_id, "is_deleted": False}
        if before:
            from datetime import datetime
            query["last_message_at"] = {"$lt": before}

        cursor = self._conv.find(
            query,
            sort=[("last_message_at", DESCENDING)],
            limit=limit + 1,    # fetch one extra to detect has_more
            projection={
                "conversation_id": 1,
                "user_id":         1,
                "title":           1,
                "summary":         1,
                "last_message":    1,
                "last_message_at": 1,
                "message_count":   1,
                "token_usage":     1,
                "created_at":      1,
                "is_deleted":      1,
            },
        )
        docs = [doc async for doc in cursor]
        has_more = len(docs) > limit
        return {
            "conversations": docs[:limit],
            "has_more":      has_more,
        }

    async def update_conversation(
        self,
        conversation_id: str,
        data:            ConversationUpdate,
    ) -> dict | None:
        updates = {k: v for k, v in data.model_dump().items() if v is not None}
        if not updates:
            return await self.get_conversation(conversation_id)
        return await self._conv.find_one_and_update(
            {"conversation_id": conversation_id},
            {"$set": updates},
            return_document=True,
        )

    async def soft_delete_conversation(self, conversation_id: str) -> None:
        await self._conv.update_one(
            {"conversation_id": conversation_id},
            {"$set": {"is_deleted": True}},
        )
        logger.info("Conversation soft deleted: %s", conversation_id)

    # ── Token tracking ────────────────────────────────────────────────────────

    async def save_token_usage(self, usage: LLMCallUsage) -> None:
        """
        Saves one LLM call to token_usage collection.
        Called after every node that invokes an LLM.
        MongoDB auto-creates _id — no custom ID needed.
        """
        await self._usage.insert_one(usage.model_dump())

    async def save_many_token_usage(self, usages: list[LLMCallUsage]) -> None:
        """
        Bulk saves all LLM calls for one user message turn.
        Called after SSE stream completes — inserts all at once.
        """
        if not usages:
            return
        await self._usage.insert_many([u.model_dump() for u in usages])

    async def update_conversation_token_totals(
        self,
        conversation_id: str,
        agent:           str,
        model:           str,
        input_tokens:    int,
        output_tokens:   int,
        total_tokens:    int,
    ) -> None:
        """
        Atomically updates pre-aggregated token totals on conversation document.
        Uses MongoDB $inc with dot-notation — creates nested fields automatically.
        Called after every LLM call — no race conditions with $inc.

        Example MongoDB update:
            $inc: {
                "token_usage.total_input_tokens": 95,
                "token_usage.by_agent.conversational_support_agent.input_tokens": 95,
                "token_usage.by_model.gpt-4o-mini.input_tokens": 95,
                ...
            }
        """
        await self._conv.update_one(
            {"conversation_id": conversation_id},
            {
                "$inc": {
                    "token_usage.total_input_tokens":                        input_tokens,
                    "token_usage.total_output_tokens":                       output_tokens,
                    "token_usage.total_tokens":                              total_tokens,
                    f"token_usage.by_agent.{agent}.input_tokens":            input_tokens,
                    f"token_usage.by_agent.{agent}.output_tokens":           output_tokens,
                    f"token_usage.by_agent.{agent}.total_tokens":            total_tokens,
                    f"token_usage.by_model.{model}.input_tokens":            input_tokens,
                    f"token_usage.by_model.{model}.output_tokens":           output_tokens,
                    f"token_usage.by_model.{model}.total_tokens":            total_tokens,
                }
            },
        )

    # ── Message CRUD ──────────────────────────────────────────────────────────

    async def save_message(self, data: MessageCreate) -> dict:
        """Saves message and updates conversation last_message metadata."""
        now = datetime.now(timezone.utc)
        doc = {
            "message_id":      data.message_id,
            "conversation_id": data.conversation_id,
            "user_id":         data.user_id,
            "role":            data.role,
            "content":         data.content,
            "sources":         data.sources,
            "ticket_url":      data.ticket_url,
            "reaction":        None,
            "created_at":      now,
            "is_deleted":      False,
        }
        await self._msgs.insert_one(doc)

        preview = data.content[:100] + "..." if len(data.content) > 100 else data.content
        await self._conv.update_one(
            {"conversation_id": data.conversation_id},
            {
                "$set": {
                    "last_message":    preview,
                    "last_message_at": now,
                },
                "$inc": {"message_count": 1},
            },
        )
        return doc

    async def get_messages(
        self,
        conversation_id: str,
        before:          datetime | None = None,
        limit:           int = PAGE_SIZE,
    ) -> PaginatedMessages:
        """
        Cursor-based paginated message fetch for infinite scroll.
        First load: no before → latest N messages
        Scroll up: before=oldest_loaded → N messages before that
        """
        query: dict = {"conversation_id": conversation_id, "is_deleted": False}
        if before:
            query["created_at"] = {"$lt": before}

        cursor = self._msgs.find(
            query,
            sort=[("created_at", DESCENDING)],
            limit=limit + 1,
        )
        docs = [doc async for doc in cursor]

        has_more  = len(docs) > limit
        page_docs = docs[:limit]
        page_docs.reverse()

        next_before = page_docs[0]["created_at"] if has_more and page_docs else None

        return PaginatedMessages(
            messages=[self._to_message_response(d) for d in page_docs],
            has_more=has_more,
            next_before=next_before,
        )

    async def update_message_reaction(
        self,
        message_id: str,
        reaction:   str,
    ) -> dict | None:
        return await self._msgs.find_one_and_update(
            {"message_id": message_id},
            {"$set": {"reaction": reaction}},
            return_document=True,
        )

    async def get_message_count(self, conversation_id: str) -> int:
        return await self._msgs.count_documents(
            {"conversation_id": conversation_id, "is_deleted": False}
        )

    # ── Mappers ───────────────────────────────────────────────────────────────

    def _to_conversation_response(self, doc: dict) -> ConversationResponse:
        raw_usage = doc.get("token_usage", {})
        return ConversationResponse(
            conversation_id=doc["conversation_id"],
            user_id=doc["user_id"],
            title=doc["title"],
            summary=doc.get("summary"),
            last_message=doc.get("last_message"),
            last_message_at=doc.get("last_message_at"),
            message_count=doc.get("message_count", 0),
            token_usage=ConversationTokenUsage(
                total_input_tokens=raw_usage.get("total_input_tokens", 0),
                total_output_tokens=raw_usage.get("total_output_tokens", 0),
                total_tokens=raw_usage.get("total_tokens", 0),
                by_agent=raw_usage.get("by_agent", {}),
                by_model=raw_usage.get("by_model", {}),
            ),
            created_at=doc["created_at"],
            is_deleted=doc.get("is_deleted", False),
        )

    def _to_message_response(self, doc: dict) -> MessageResponse:
        return MessageResponse(
            message_id=doc["message_id"],
            conversation_id=doc["conversation_id"],
            user_id=doc["user_id"],
            role=doc["role"],
            content=doc["content"],
            sources=doc.get("sources"),
            ticket_url=doc.get("ticket_url"),
            reaction=doc.get("reaction"),
            created_at=doc["created_at"],
            is_deleted=doc.get("is_deleted", False),
        )