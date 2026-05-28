# app/api/v1/chat.py
# Chat API — HTTP layer only.
#
# Responsibilities:
#   - Define FastAPI routes
#   - Validate HTTP requests (Pydantic schemas)
#   - Inject dependencies (ConversationSvc, CurrentUser)
#   - Create ChatPipeline and hand off to it
#   - Manage _running_tasks for stop-stream support
#
# NO business logic here. All pipeline logic lives in:
#   app/agents/pipeline/orchestrator.py  — stage sequencing
#   app/agents/pipeline/classifier.py   — intent detection
#   app/agents/pipeline/responses.py    — non-search responses
#   app/agents/pipeline/formatter.py    — markdown formatting
#   app/agents/pipeline/post_processor.py — cleanup
#   app/agents/pipeline/suggestions.py  — follow-up questions
#   app/agents/graph/checkpoint_repair.py — checkpoint repair
#
# Endpoints:
#   POST   /                            — SSE stream (main chat)
#   POST   /sync                        — non-streaming (testing only)
#   POST   /stop                        — cancel active stream
#   GET    /sessions                    — paginated conversation list
#   POST   /sessions                    — create new session
#   DELETE /sessions/{id}               — soft delete
#   GET    /sessions/{id}/messages      — paginated messages
#   PATCH  /sessions/{id}/title         — rename conversation
#   POST   /messages/{id}/reaction      — thumbs up/down
#
# workers=1 MANDATORY in uvicorn — _running_tasks dict is process-local.
# Scale horizontally via AKS pods, not uvicorn workers.

import asyncio
import logging
from datetime import datetime

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.agents.pipeline.models       import PipelineRequest
from app.agents.pipeline.orchestrator import ChatPipeline
from app.domains.auth.dependencies    import CurrentUser
from app.domains.conversations.schemas import MessageReactionUpdate
from app.dependencies                 import ConversationSvc
from app.exceptions                   import NotFoundError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["Chat"])

# session_id → asyncio.Task
# workers=1 MANDATORY — this dict is process-local
_running_tasks: dict[str, asyncio.Task] = {}


# ── Pydantic request schemas ──────────────────────────────────────────────────
# Pydantic used here because these are at the API boundary (external input).

class ChatRequest(BaseModel):
    session_id: str
    message:    str

class StopRequest(BaseModel):
    session_id: str

class ChatSyncRequest(BaseModel):
    session_id: str
    message:    str

class TitleUpdate(BaseModel):
    title: str


# ── SSE stream endpoint ───────────────────────────────────────────────────────

@router.post("/")
async def chat(
    request:      ChatRequest,
    user:         CurrentUser,
    conversation: ConversationSvc,
) -> StreamingResponse:
    """
    Main chat endpoint — returns Server-Sent Events stream.
    Delegates all logic to ChatPipeline.
    """
    pipeline_request = PipelineRequest(
        session_id=request.session_id,
        user_message=request.message.strip(),
        user_id=user.user_id,
        user_name=getattr(user, "name", ""),
    )

    pipeline = ChatPipeline(conversation_svc=conversation)

    async def event_generator():
        agent_task = None
        try:
            async for event in pipeline.run(
                request=pipeline_request,
                running_tasks=_running_tasks,
            ):
                yield event
        finally:
            _running_tasks.pop(request.session_id, None)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":        "keep-alive",
        },
    )


# ── Stop endpoint ─────────────────────────────────────────────────────────────

@router.post("/stop")
async def stop_stream(
    request: StopRequest,
    user:    CurrentUser,
) -> dict:
    task = _running_tasks.get(request.session_id)
    if task and not task.done():
        task.cancel()
        logger.info(
            "Stream stopped by %s for session %s",
            user.user_id, request.session_id,
        )
        return {"status": "stopped", "session_id": request.session_id}
    return {"status": "not_running", "session_id": request.session_id}


# ── Non-streaming endpoint (testing only) ─────────────────────────────────────

@router.post("/sync")
async def chat_sync(
    request:      ChatSyncRequest,
    user:         CurrentUser,
    conversation: ConversationSvc,
) -> dict:
    """
    Non-streaming chat — collects full SSE stream and returns final content.
    For testing and debugging only. Not used in production UI.
    """
    pipeline_request = PipelineRequest(
        session_id=request.session_id,
        user_message=request.message.strip(),
        user_id=user.user_id,
        user_name=getattr(user, "name", ""),
    )

    pipeline = ChatPipeline(conversation_svc=conversation)

    content    = ""
    ticket_url = None
    message_id = None

    async for event_str in pipeline.run(
        request=pipeline_request,
        running_tasks={},  # isolated — stop not supported for sync
    ):
        if event_str.startswith(": heartbeat"):
            continue
        if "event: token" in event_str:
            import json
            data_line = [l for l in event_str.split("\n") if l.startswith("data:")]
            if data_line:
                data = json.loads(data_line[0][5:])
                content += data.get("token", "")
        elif "event: done" in event_str:
            import json
            data_line = [l for l in event_str.split("\n") if l.startswith("data:")]
            if data_line:
                data       = json.loads(data_line[0][5:])
                ticket_url = data.get("ticket_url")
                message_id = data.get("message_id")

    return {
        "message_id": message_id,
        "content":    content,
        "ticket_url": ticket_url,
        "session_id": request.session_id,
    }


# ── Session management ────────────────────────────────────────────────────────

@router.get("/sessions")
async def get_sessions(
    user:         CurrentUser,
    conversation: ConversationSvc,
    before:       str | None = None,
    limit:        int        = 20,
) -> dict:
    return await conversation.get_user_sessions(
        user_id=user.user_id, limit=limit, before=before,
    )


@router.post("/sessions")
async def create_session(
    user:         CurrentUser,
    conversation: ConversationSvc,
) -> dict:
    session = await conversation.create_session(user_id=user.user_id)
    return session.model_dump()


@router.delete("/sessions/{conversation_id}")
async def delete_session(
    conversation_id: str,
    user:            CurrentUser,
    conversation:    ConversationSvc,
) -> dict:
    await conversation.delete_session(
        conversation_id=conversation_id,
        user_id=user.user_id,
    )
    return {"status": "deleted", "conversation_id": conversation_id}


@router.get("/sessions/{conversation_id}/messages")
async def get_messages(
    conversation_id: str,
    user:            CurrentUser,
    conversation:    ConversationSvc,
    before:          str | None = None,
) -> dict:
    before_dt = datetime.fromisoformat(before) if before else None
    result    = await conversation.get_messages(
        conversation_id=conversation_id,
        before=before_dt,
    )
    return result.model_dump()


@router.patch("/sessions/{conversation_id}/title")
async def update_title(
    conversation_id: str,
    body:            TitleUpdate,
    user:            CurrentUser,
    conversation:    ConversationSvc,
) -> dict:
    await conversation.update_title(
        conversation_id=conversation_id,
        title=body.title,
    )
    return {"status": "updated", "conversation_id": conversation_id}


@router.post("/messages/{message_id}/reaction")
async def update_reaction(
    message_id:   str,
    body:         MessageReactionUpdate,
    user:         CurrentUser,
    conversation: ConversationSvc,
) -> dict:
    result = await conversation.update_reaction(
        message_id=message_id,
        reaction=body.reaction,
    )
    if not result:
        raise NotFoundError("Message", message_id)
    return result.model_dump()