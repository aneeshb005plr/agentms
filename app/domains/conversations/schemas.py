# app/domains/conversations/schemas.py
# Pydantic models for conversations, messages, and token tracking.
#
# Two main collections:
#   conversations  — session metadata, sidebar display, agent-wise token summary
#   messages       — individual messages, paginated, no token detail
#
# One audit collection:
#   token_usage    — one document per LLM call, granular audit trail
#
# conversation_id = LangGraph thread_id — one field ties everything together

from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field
import uuid


# ── Token tracking schemas ────────────────────────────────────────────────────

class LLMCallUsage(BaseModel):
    """
    One LLM call from one node inside one agent.
    Saved to token_usage collection — one document per LLM call.
    MongoDB auto-creates _id — no custom ID needed.
    """
    conversation_id: str
    message_id:      str
    user_id:         str
    agent:           str        # e.g. "conversational_support_agent"
    node:            str        # e.g. "intent_classifier"
    model:           str        # e.g. "gpt-4o-mini"
    input_tokens:    int
    output_tokens:   int
    total_tokens:    int
    timestamp:       datetime = Field(default_factory=datetime.utcnow)


class AgentTokenSummary(BaseModel):
    """Token summary per agent — stored inside conversation document."""
    input_tokens:  int = 0
    output_tokens: int = 0
    total_tokens:  int = 0


class ModelTokenSummary(BaseModel):
    """Token summary per model — stored inside conversation document."""
    input_tokens:  int = 0
    output_tokens: int = 0
    total_tokens:  int = 0


class ConversationTokenUsage(BaseModel):
    """
    Pre-aggregated token summary stored on conversations document.
    Updated atomically via MongoDB $inc after every LLM call.
    by_agent and by_model keys are dynamic — added as new agents/models appear.
    """
    total_input_tokens:  int = 0
    total_output_tokens: int = 0
    total_tokens:        int = 0
    by_agent: dict[str, AgentTokenSummary] = {}   # agent_name → totals
    by_model: dict[str, ModelTokenSummary] = {}   # model_name → totals


# ── Conversation schemas ──────────────────────────────────────────────────────

class ConversationCreate(BaseModel):
    """Created when user starts a new chat session."""
    conversation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id:         str
    title:           str = "New Conversation"


class ConversationUpdate(BaseModel):
    """Partial update — PATCH semantics, only provided fields updated."""
    title:           str | None = None
    summary:         str | None = None
    last_message:    str | None = None
    last_message_at: datetime | None = None
    message_count:   int | None = None


class ConversationResponse(BaseModel):
    """Returned to Angular sidebar — lean, no messages included."""
    conversation_id:  str
    user_id:          str
    title:            str
    summary:          str | None
    last_message:     str | None
    last_message_at:  datetime | None
    message_count:    int
    token_usage:      ConversationTokenUsage
    created_at:       datetime
    is_deleted:       bool


# ── Message schemas ───────────────────────────────────────────────────────────

class MessageCreate(BaseModel):
    """Created when user sends or agent responds."""
    message_id:      str = Field(default_factory=lambda: str(uuid.uuid4()))
    conversation_id: str
    user_id:         str
    role:            Literal["user", "assistant"]
    content:         str
    sources:         list[dict] | None = None   # vector search sources
    ticket_url:      str | None = None          # ServiceNow link if applicable


class MessageReactionUpdate(BaseModel):
    """User thumbs up/down on an AI message."""
    reaction: Literal["thumbs_up", "thumbs_down"]


class MessageResponse(BaseModel):
    """Individual message returned to Angular."""
    message_id:      str
    conversation_id: str
    user_id:         str
    role:            Literal["user", "assistant"]
    content:         str
    sources:         list[dict] | None
    ticket_url:      str | None
    reaction:        str | None
    created_at:      datetime
    is_deleted:      bool


# ── Paginated response ────────────────────────────────────────────────────────

class PaginatedMessages(BaseModel):
    """Paginated message list — cursor-based infinite scroll."""
    messages:    list[MessageResponse]
    has_more:    bool
    next_before: datetime | None   # cursor for next page (scroll up)