# app/api/v1/chat.py
# Chat API — SSE streaming endpoint + session management.
#
# Endpoints:
#   POST   /api/v1/chat                              — start SSE stream
#   POST   /api/v1/chat/stop                         — cancel running stream
#   GET    /api/v1/chat/sessions                     — list user conversations
#   POST   /api/v1/chat/sessions                     — create new session
#   DELETE /api/v1/chat/sessions/{conversation_id}   — soft delete session
#   GET    /api/v1/chat/sessions/{id}/messages       — paginated messages
#   PATCH  /api/v1/chat/sessions/{id}/title          — rename conversation
#   POST   /api/v1/chat/messages/{id}/reaction       — thumbs up/down
#
# SSE event types emitted to Angular:
#   agent_thinking  → { status: str, node: str }         — agent working
#   tool_call       → { tool: str, query: str }           — tool invoked
#   tool_result     → { tool: str, found: bool }          — tool returned
#   token           → { token: str }                      — LLM streaming token
#   done            → { message_id: str, ticket_url: str|None }
#   error           → { message: str }
#   heartbeat       → {}                                  — keep-alive (every 15s)
#
# Stop/cancel:
#   running_tasks dict maps session_id → asyncio.Task
#   POST /chat/stop cancels the task
#   workers=1 in uvicorn is MANDATORY — dict lives in process memory
#   Scale via AKS pods (horizontal), not uvicorn workers (vertical)
#
# Token tracking:
#   After stream completes, reads state.current_message_llm_calls
#   Calls ConversationService.record_llm_calls() to save audit trail
#   Updates conversation token totals atomically

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent
from langchain_core.messages import HumanMessage
from pydantic import BaseModel

from app.agents.graph.master_graph import master_graph
from app.agents.graph.subgraphs.conversational_support_agent import extract_app_identified
from app.domains.auth.dependencies import CurrentUser
from app.domains.conversations.schemas import MessageReactionUpdate
from app.dependencies import ConversationSvc
from app.exceptions import NotFoundError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["Chat"])

# ── Running tasks registry ────────────────────────────────────────────────────
# session_id → asyncio.Task
# workers=1 MANDATORY — this dict lives in process memory
# Scale horizontally via AKS pods, not uvicorn workers
_running_tasks: dict[str, asyncio.Task] = {}

# Heartbeat interval — must be less than Ocelot/AKS proxy timeout
_HEARTBEAT_INTERVAL = 15   # seconds


# ── Request / Response schemas ────────────────────────────────────────────────

class ChatRequest(BaseModel):
    session_id:  str
    message:     str


class StopRequest(BaseModel):
    session_id: str


class TitleUpdate(BaseModel):
    title: str


# ── SSE helpers ───────────────────────────────────────────────────────────────

def _sse_event(event_type: str, data: dict) -> ServerSentEvent:
    """Formats a typed SSE event."""
    return ServerSentEvent(
        event=event_type,
        data=json.dumps(data),
    )


# ── SSE Stream endpoint ───────────────────────────────────────────────────────

@router.post("/")
async def chat(
    request:      ChatRequest,
    user:         CurrentUser,
    conversation: ConversationSvc,
) -> EventSourceResponse:
    """
    Main chat SSE endpoint.
    Starts LangGraph agent stream and emits real-time events to Angular.

    Flow:
    1. Save user message to messages collection
    2. Start agent stream as asyncio.Task (cancellable)
    3. Register task in running_tasks
    4. Emit SSE events as agent processes
    5. On completion: save assistant message + token usage
    6. On stop/cancel: save partial response
    """

    async def event_generator() -> AsyncGenerator[ServerSentEvent, None]:
        session_id      = request.session_id
        user_message    = request.message.strip()
        message_id      = None
        full_response   = []
        ticket_url      = None
        llm_calls       = []
        event_queue:    asyncio.Queue = asyncio.Queue()

        # ── Save user message ─────────────────────────────────────────────────
        await conversation.save_user_message(
            conversation_id=session_id,
            user_id=user.user_id,
            content=user_message,
        )

        # ── Agent stream task ─────────────────────────────────────────────────
        async def run_agent():
            """Runs LangGraph agent and pushes events to queue."""
            try:
                config = {
                    "configurable": {
                        "thread_id": session_id,
                    }
                }

                initial_input = {
                    "messages":                    [HumanMessage(content=user_message)],
                    "session_id":                  session_id,
                    "user_id":                     user.user_id,
                    "current_message_llm_calls":   [],
                    "requires_ticket":             False,
                    "search_results":              None,
                    "user_intent":                 None,
                    "search_queries":              None,
                    "app_identified":              None,
                    "health_data":                 None,
                    "conversation_summary":        None,
                    "retrieved_memory":            None,
                }

                async for event in master_graph.graph.astream_events(
                    input=initial_input,
                    config=config,
                    version="v2",
                ):
                    event_name = event.get("event", "")
                    metadata   = event.get("metadata", {})
                    node_name  = metadata.get("langgraph_node", "")

                    # ── LLM token streaming ───────────────────────────────────
                    if event_name == "on_chat_model_stream":
                        chunk = event.get("data", {}).get("chunk")
                        if chunk and hasattr(chunk, "content") and chunk.content:
                            token = chunk.content
                            full_response.append(token)
                            await event_queue.put(
                                _sse_event("token", {"token": token})
                            )

                    # ── Agent thinking (node started) ─────────────────────────
                    elif event_name == "on_chain_start" and node_name:
                        status_map = {
                            "agent":   "Thinking...",
                            "tools":   "Executing tool...",
                        }
                        status = status_map.get(node_name, f"Processing ({node_name})...")
                        await event_queue.put(
                            _sse_event("agent_thinking", {
                                "status": status,
                                "node":   node_name,
                            })
                        )

                    # ── Tool call started ─────────────────────────────────────
                    elif event_name == "on_tool_start":
                        tool_name = event.get("name", "")
                        tool_input = event.get("data", {}).get("input", {})
                        query = tool_input.get("query", tool_input.get("app_name", ""))
                        await event_queue.put(
                            _sse_event("tool_call", {
                                "tool":  tool_name,
                                "query": str(query),
                            })
                        )

                    # ── Tool call completed ───────────────────────────────────
                    elif event_name == "on_tool_end":
                        tool_name   = event.get("name", "")
                        tool_output = event.get("data", {}).get("output", "")

                        # Detect if ServiceNow link was returned
                        nonlocal ticket_url
                        if tool_name == "get_servicenow_link" and "SERVICENOW_LINK:" in str(tool_output):
                            ticket_url = tool_output.split("SERVICENOW_LINK:")[-1].strip().split("\n")[0]

                        found = "NO_RESULTS_FOUND" not in str(tool_output)
                        await event_queue.put(
                            _sse_event("tool_result", {
                                "tool":  tool_name,
                                "found": found,
                            })
                        )

                    # ── LLM call completed — collect token usage ──────────────
                    elif event_name == "on_chat_model_end":
                        output = event.get("data", {}).get("output")
                        if output and hasattr(output, "usage_metadata") and output.usage_metadata:
                            response_meta = getattr(output, "response_metadata", {})
                            model = response_meta.get("model_name", "unknown")
                            llm_calls.append({
                                "agent": "conversational_support_agent",
                                "node":  node_name or "agent_loop",
                                "model": model,
                                "input_tokens":  output.usage_metadata.get("input_tokens", 0),
                                "output_tokens": output.usage_metadata.get("output_tokens", 0),
                                "total_tokens":  output.usage_metadata.get("total_tokens", 0),
                            })

                # Signal completion
                await event_queue.put(None)

            except asyncio.CancelledError:
                logger.info("Agent stream cancelled for session: %s", session_id)
                await event_queue.put("CANCELLED")

            except Exception as e:
                logger.error("Agent stream error for session %s: %s", session_id, str(e))
                await event_queue.put(
                    _sse_event("error", {"message": "An error occurred. Please try again."})
                )
                await event_queue.put(None)

        # ── Heartbeat task ────────────────────────────────────────────────────
        async def heartbeat():
            """Sends heartbeat every 15s to prevent proxy timeout."""
            while True:
                await asyncio.sleep(_HEARTBEAT_INTERVAL)
                await event_queue.put(
                    _sse_event("heartbeat", {})
                )

        # ── Start tasks ───────────────────────────────────────────────────────
        agent_task     = asyncio.create_task(run_agent())
        heartbeat_task = asyncio.create_task(heartbeat())
        _running_tasks[request.session_id] = agent_task

        try:
            # ── Consume queue and yield SSE events ────────────────────────────
            while True:
                item = await event_queue.get()

                if item is None:
                    # Stream complete
                    break

                if item == "CANCELLED":
                    # Stream cancelled by user
                    yield _sse_event("error", {"message": "Response stopped."})
                    break

                yield item

            # ── Post-stream: save assistant message + token usage ─────────────
            final_content = "".join(full_response)

            if final_content:
                saved = await conversation.save_assistant_message(
                    conversation_id=request.session_id,
                    user_id=user.user_id,
                    content=final_content,
                    ticket_url=ticket_url,
                )
                message_id = saved.message_id

                # Save token usage audit trail + update conversation totals
                if llm_calls:
                    await conversation.record_llm_calls(
                        conversation_id=request.session_id,
                        message_id=message_id,
                        user_id=user.user_id,
                        llm_calls=llm_calls,
                    )

                # Check if summary needed (Layer 2 memory)
                if await conversation.should_generate_summary(request.session_id):
                    logger.info(
                        "Summary trigger reached for session: %s",
                        request.session_id,
                    )
                    # Summary generation handled by agent on next turn
                    # ConversationService.update_summary() called by agent

                # Emit done event
                yield _sse_event("done", {
                    "message_id": message_id,
                    "ticket_url": ticket_url,
                })

            # Auto-generate title after first message
            sessions = await conversation.get_user_sessions(user.user_id)
            current = next(
                (s for s in sessions if s.conversation_id == request.session_id),
                None,
            )
            if current and current.message_count <= 2 and current.title == "New Conversation":
                # Title generation — use first 50 chars of user message as fallback
                # Full title generation via fast LLM happens in Week 3 refinement
                auto_title = user_message[:50] + ("..." if len(user_message) > 50 else "")
                await conversation.update_title(request.session_id, auto_title)

        finally:
            # ── Cleanup ───────────────────────────────────────────────────────
            heartbeat_task.cancel()
            agent_task.cancel()
            _running_tasks.pop(request.session_id, None)

            await asyncio.gather(
                heartbeat_task,
                agent_task,
                return_exceptions=True,
            )

    return EventSourceResponse(event_generator())


# ── Stop endpoint ─────────────────────────────────────────────────────────────

@router.post("/stop")
async def stop_stream(
    request: StopRequest,
    user:    CurrentUser,
) -> dict:
    """
    Cancels a running agent stream for the given session.
    Angular calls this when user clicks the Stop button.
    """
    task = _running_tasks.get(request.session_id)

    if task and not task.done():
        task.cancel()
        logger.info(
            "Stream stopped by user %s for session %s",
            user.user_id, request.session_id,
        )
        return {"status": "stopped", "session_id": request.session_id}

    return {"status": "not_running", "session_id": request.session_id}


# ── Session management endpoints ──────────────────────────────────────────────

@router.get("/sessions")
async def get_sessions(
    user:         CurrentUser,
    conversation: ConversationSvc,
) -> list:
    """Returns all conversations for the sidebar — newest first."""
    sessions = await conversation.get_user_sessions(user.user_id)
    return [s.model_dump() for s in sessions]


@router.post("/sessions")
async def create_session(
    user:         CurrentUser,
    conversation: ConversationSvc,
) -> dict:
    """Creates a new conversation session."""
    session = await conversation.create_session(user_id=user.user_id)
    return session.model_dump()


@router.delete("/sessions/{conversation_id}")
async def delete_session(
    conversation_id: str,
    user:            CurrentUser,
    conversation:    ConversationSvc,
) -> dict:
    """Soft deletes a conversation — verifies ownership."""
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
    before:          str | None = None,   # ISO datetime cursor for pagination
) -> dict:
    """
    Paginated message fetch for infinite scroll.
    First load: no before param → latest 30 messages.
    Scroll up: before=ISO datetime → 30 messages before that timestamp.
    """
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
    """Updates conversation title — user rename."""
    await conversation.update_title(
        conversation_id=conversation_id,
        title=body.title,
    )
    return {"status": "updated", "conversation_id": conversation_id}


@router.post("/messages/{message_id}/reaction")
async def update_reaction(
    message_id: str,
    body:       MessageReactionUpdate,
    user:       CurrentUser,
    conversation: ConversationSvc,
) -> dict:
    """Thumbs up / thumbs down on an AI message."""
    result = await conversation.update_reaction(
        message_id=message_id,
        reaction=body.reaction,
    )
    if not result:
        raise NotFoundError("Message", message_id)
    return result.model_dump()