# app/domains/conversations/service.py
# Business logic for conversation, message, and token usage management.
#
# Token tracking flow:
#   1. Agent node executes LLM call
#   2. Node appends entry to state.current_message_llm_calls via operator.add
#   3. SSE endpoint reads state after stream completes
#   4. Calls record_llm_calls() with all accumulated entries
#   5. record_llm_calls():
#       a. Bulk saves all entries to token_usage collection (audit trail)
#       b. Updates conversation token totals per agent and model (summary)

import logging
import uuid
from datetime import datetime

from pymongo.asynchronous.database import AsyncDatabase

from app.config import settings
from app.domains.conversations.repository import ConversationRepository
from app.domains.prompts.cache import prompt_cache
from app.domains.conversations.schemas import (
    ConversationCreate,
    ConversationResponse,
    ConversationUpdate,
    LLMCallUsage,
    MessageCreate,
    MessageResponse,
    PaginatedMessages,
)

logger = logging.getLogger(__name__)


class ConversationService:

    def __init__(self, db: AsyncDatabase):
        self._repo = ConversationRepository(db)

    # ── Session management ────────────────────────────────────────────────────

    async def create_session(self, user_id: str) -> ConversationResponse:
        """
        Creates a new conversation session.
        Returns conversation_id which becomes the LangGraph thread_id.
        Called when user clicks New Conversation in Angular.
        """
        data = ConversationCreate(
            conversation_id=str(uuid.uuid4()),
            user_id=user_id,
            title="New Conversation",
        )
        doc = await self._repo.create_conversation(data)
        return self._repo._to_conversation_response(doc)

    async def get_user_sessions(self, user_id: str) -> list[ConversationResponse]:
        """Returns all conversations for sidebar — newest first."""
        docs = await self._repo.get_user_conversations(
            user_id=user_id,
            limit=settings.CONVERSATION_HISTORY_LIMIT,
        )
        return [self._repo._to_conversation_response(d) for d in docs]

    async def delete_session(
        self,
        conversation_id: str,
        user_id:         str,
    ) -> None:
        """Soft deletes a conversation — verifies ownership first."""
        doc = await self._repo.get_conversation(conversation_id)
        if not doc or doc["user_id"] != user_id:
            from app.exceptions import NotFoundError
            raise NotFoundError("Conversation", conversation_id)
        await self._repo.soft_delete_conversation(conversation_id)

    async def update_title(self, conversation_id: str, title: str) -> None:
        """Updates title — after auto-generation or user rename."""
        await self._repo.update_conversation(
            conversation_id,
            ConversationUpdate(title=title),
        )

    async def update_summary(self, conversation_id: str, summary: str) -> None:
        """Stores rolling summary — Layer 2 memory."""
        await self._repo.update_conversation(
            conversation_id,
            ConversationUpdate(summary=summary),
        )

    # ── Message management ────────────────────────────────────────────────────

    async def save_user_message(
        self,
        conversation_id: str,
        user_id:         str,
        content:         str,
    ) -> MessageResponse:
        """Saves user message. Updates conversation last_message metadata."""
        doc = await self._repo.save_message(MessageCreate(
            conversation_id=conversation_id,
            user_id=user_id,
            role="user",
            content=content,
        ))
        return self._repo._to_message_response(doc)

    async def save_assistant_message(
        self,
        conversation_id: str,
        user_id:         str,
        content:         str,
        sources:         list[dict] | None = None,
        ticket_url:      str | None = None,
    ) -> MessageResponse:
        """
        Saves assistant response after SSE stream completes.
        Returns message_id used to link token_usage records.
        """
        doc = await self._repo.save_message(MessageCreate(
            conversation_id=conversation_id,
            user_id=user_id,
            role="assistant",
            content=content,
            sources=sources,
            ticket_url=ticket_url,
        ))
        return self._repo._to_message_response(doc)

    async def get_messages(
        self,
        conversation_id: str,
        before:          datetime | None = None,
    ) -> PaginatedMessages:
        """Paginated message fetch for infinite scroll in Angular."""
        return await self._repo.get_messages(
            conversation_id=conversation_id,
            before=before,
        )

    async def update_reaction(
        self,
        message_id: str,
        reaction:   str,
    ) -> MessageResponse | None:
        """Thumbs up/down on AI message."""
        doc = await self._repo.update_message_reaction(message_id, reaction)
        return self._repo._to_message_response(doc) if doc else None

    # ── Token tracking ────────────────────────────────────────────────────────

    async def record_llm_calls(
        self,
        conversation_id:   str,
        message_id:        str,
        user_id:           str,
        llm_calls:         list[dict],
    ) -> None:
        """
        Records all LLM calls from one user message turn.
        Called by SSE endpoint after stream completes with
        state.current_message_llm_calls.

        Steps:
        1. Converts state dicts to LLMCallUsage models
        2. Bulk saves all to token_usage collection (audit trail)
        3. Updates conversation token totals per agent and model (summary)

        Each entry in llm_calls:
            {
                "agent": "conversational_support_agent",
                "node":  "intent_classifier",
                "model": "gpt-4o-mini",
                "input_tokens":  95,
                "output_tokens": 12,
                "total_tokens":  107
            }
        """
        if not llm_calls:
            return

        # Build LLMCallUsage models
        usage_records = [
            LLMCallUsage(
                conversation_id=conversation_id,
                message_id=message_id,
                user_id=user_id,
                agent=call["agent"],
                node=call["node"],
                model=call["model"],
                input_tokens=call["input_tokens"],
                output_tokens=call["output_tokens"],
                total_tokens=call["total_tokens"],
            )
            for call in llm_calls
        ]

        # 1. Bulk save all to token_usage collection
        await self._repo.save_many_token_usage(usage_records)

        # 2. Update conversation totals per agent and model
        for record in usage_records:
            await self._repo.update_conversation_token_totals(
                conversation_id=conversation_id,
                agent=record.agent,
                model=record.model,
                input_tokens=record.input_tokens,
                output_tokens=record.output_tokens,
                total_tokens=record.total_tokens,
            )

        logger.info(
            "Token usage recorded: conversation=%s message=%s calls=%d",
            conversation_id, message_id, len(llm_calls),
        )

    # ── Summary trigger ───────────────────────────────────────────────────────

    async def generate_title_if_needed(
        self,
        conversation_id: str,
        first_user_message: str,
    ) -> None:
        """
        Auto-generates conversation title using fast LLM (gpt-4o-mini).
        Called after saving first assistant message.
        Only updates if title is still "New Conversation".
        Falls back to first 60 chars if LLM call fails.
        """
        doc = await self._repo.get_conversation(conversation_id)
        if not doc:
            return

        # Only update if title is still default
        if doc.get("title", "") != "New Conversation":
            return

        # Try LLM title generation first
        title = await self._generate_title_with_llm(first_user_message)

        await self._repo.update_conversation(
            conversation_id,
            ConversationUpdate(title=title),
        )
        logger.info("Auto-title generated for %s: %s", conversation_id, title)

    async def _generate_title_with_llm(self, user_message: str) -> str:
        """
        Uses fast LLM (gpt-4o-mini) to generate a short conversation title.
        Falls back to first 60 chars if LLM fails.
        """
        try:
            from app.agents.clients.llm_client import llm_client
            from app.domains.prompts.service import PromptService
            from app.domains.prompts.cache import PromptCache
            from langchain_core.messages import HumanMessage, SystemMessage

            # Load title prompt from cache (already loaded at startup)
            prompt_content = prompt_cache.get(
                PromptCache.TITLE_GENERATION,
                PromptCache.TITLE_PROMPT,
            )

            if not prompt_content:
                raise ValueError("Title prompt not in cache")

            # Format prompt with user message
            formatted = prompt_content.replace("{message}", user_message[:500])

            # Call fast LLM — gpt-4o-mini — quick and cheap
            response = await llm_client.fast.ainvoke([
                HumanMessage(content=formatted)
            ])

            title = response.content.strip()

            # Sanitise — remove quotes, limit length
            title = title.strip('"'').strip()
            if len(title) > 60:
                title = title[:60] + "..."
            if not title:
                raise ValueError("Empty title from LLM")

            return title

        except Exception as e:
            logger.warning(
                "LLM title generation failed: %s — using fallback", str(e)
            )
            # Fallback — first 60 chars of user message
            fallback = user_message.strip()
            if len(fallback) > 60:
                fallback = fallback[:60] + "..."
            return fallback

    async def should_generate_summary(self, conversation_id: str) -> bool:
        """
        Returns True when message count hits SUMMARY_TRIGGER_COUNT threshold.
        Agent generates rolling summary and calls update_summary() when True.
        """
        count = await self._repo.get_message_count(conversation_id)
        return count > 0 and count % settings.SUMMARY_TRIGGER_COUNT == 0