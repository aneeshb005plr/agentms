# app/api/v1/chat.py
# Chat API — SSE streaming endpoint + session management.
#
# SSE Implementation:
#   Uses StreamingResponse with manually formatted SSE strings.
#   Works on ALL FastAPI versions — no fastapi.sse dependency needed.
#   Format: "event: {type}\ndata: {json}\n\n"
#
# Endpoints:
#   POST   /api/v1/chat/                            — start SSE stream
#   POST   /api/v1/chat/stop                        — cancel running stream
#   GET    /api/v1/chat/sessions                    — list user conversations
#   POST   /api/v1/chat/sessions                    — create new session
#   DELETE /api/v1/chat/sessions/{id}               — soft delete session
#   GET    /api/v1/chat/sessions/{id}/messages      — paginated messages
#   PATCH  /api/v1/chat/sessions/{id}/title         — rename conversation
#   POST   /api/v1/chat/messages/{id}/reaction      — thumbs up/down
#
# SSE event types:
#   agent_thinking  → { status, node }
#   tool_call       → { tool, query }
#   tool_result     → { tool, found }
#   token           → { token }
#   done            → { message_id, ticket_url }
#   error           → { message }
#   heartbeat       → {}
#
# Stop/cancel:
#   _running_tasks dict maps session_id → asyncio.Task
#   workers=1 MANDATORY — dict lives in process memory

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from datetime import datetime

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
from pydantic import BaseModel

from app.agents.graph.master_graph import master_graph
from app.domains.auth.dependencies import CurrentUser
from app.domains.conversations.schemas import MessageReactionUpdate
from app.dependencies import ConversationSvc
from app.exceptions import NotFoundError

logger = logging.getLogger(__name__)
router  = APIRouter(prefix="/chat", tags=["Chat"])


async def _repair_checkpoint_after_cancel(session_id: str) -> None:
    """
    Repairs LangGraph checkpoint after a cancelled stream.

    Problem:
        When stream is cancelled mid-tool-call, the checkpoint contains:
            AIMessage(tool_calls=[...])   ← tool call started
            # ToolMessage MISSING         ← stream cancelled before result

        Next LLM call fails with HTTP 400:
        "tool_calls must be followed by tool messages"

    Fix:
        Load the checkpoint messages and remove any AIMessage that has
        tool_calls but is not followed by the corresponding ToolMessages.
        This gives the next conversation a clean slate.
    """
    try:
        from app.agents.graph.master_graph import master_graph
        from langchain_core.messages import AIMessage, ToolMessage

        config = {"configurable": {"thread_id": session_id}}

        # Get current checkpoint state
        state = await master_graph.graph.aget_state(config)
        if not state or not state.values:
            return

        messages = state.values.get("messages", [])
        if not messages:
            return

        # Find dangling tool calls — AIMessage with tool_calls not followed by ToolMessages
        cleaned = list(messages)
        i = len(cleaned) - 1

        while i >= 0:
            msg = cleaned[i]

            # Check if this is an AIMessage with tool_calls
            if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
                tool_call_ids = {tc["id"] for tc in msg.tool_calls}

                # Check if all tool_calls have corresponding ToolMessages after this index
                tool_response_ids = {
                    m.tool_call_id
                    for m in cleaned[i + 1:]
                    if isinstance(m, ToolMessage) and hasattr(m, "tool_call_id")
                }

                # If any tool_call_id is missing a response — remove this AIMessage
                # and any partial ToolMessages after it
                if not tool_call_ids.issubset(tool_response_ids):
                    logger.warning(
                        "Repairing checkpoint for session %s — "
                        "removing dangling AIMessage with tool_calls at index %d",
                        session_id, i
                    )
                    # Remove from this AIMessage onwards
                    cleaned = cleaned[:i]
                    break

            i -= 1

        if len(cleaned) != len(messages):
            # Update checkpoint with cleaned messages
            await master_graph.graph.aupdate_state(
                config,
                {"messages": cleaned},
            )
            logger.info(
                "Checkpoint repaired for session %s — "
                "removed %d dangling messages",
                session_id, len(messages) - len(cleaned)
            )

    except Exception as e:
        # Never let checkpoint repair crash the next request
        logger.warning("Checkpoint repair failed for session %s: %s", session_id, str(e))

# ── Running tasks registry ────────────────────────────────────────────────────
# session_id → asyncio.Task
# workers=1 MANDATORY — this dict lives in process memory
_running_tasks: dict[str, asyncio.Task] = {}

# Heartbeat interval — must be less than proxy timeout
_HEARTBEAT_INTERVAL = 15  # seconds


# ── Request schemas ───────────────────────────────────────────────────────────

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


# ── SSE formatting helpers ────────────────────────────────────────────────────

def _fmt(event_type: str, data: dict) -> str:
    """
    Formats a typed SSE event as a raw string.
    Format: event: {type}\ndata: {json}\n\n
    Works with ALL FastAPI/Starlette versions.
    """
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def _heartbeat() -> str:
    """SSE comment — keeps connection alive without triggering event handlers."""
    return ": heartbeat\n\n"


# ── SSE Stream endpoint ───────────────────────────────────────────────────────

@router.post("/")
async def chat(
    request:      ChatRequest,
    user:         CurrentUser,
    conversation: ConversationSvc,
) -> StreamingResponse:
    """
    Main chat SSE endpoint.
    Returns StreamingResponse with text/event-stream content type.
    """

    async def event_generator() -> AsyncGenerator[str, None]:
        session_id    = request.session_id
        user_message  = request.message.strip()
        message_id    = None
        full_response: list[str] = []
        ticket_url:   str | None = None
        llm_calls:    list[dict] = []
        event_queue:  asyncio.Queue = asyncio.Queue()

        # ── Repair checkpoint if previous stream was cancelled mid-tool-call ──
        # Prevents HTTP 400 "tool_calls must be followed by tool messages"
        await _repair_checkpoint_after_cancel(session_id)

        # ── Save user message ─────────────────────────────────────────────────
        await conversation.save_user_message(
            conversation_id=session_id,
            user_id=user.user_id,
            content=user_message,
        )

        # ── Agent stream task ─────────────────────────────────────────────────
        async def run_agent() -> None:
            try:
                config = {"configurable": {"thread_id": session_id}}

                initial_input = {
                    "messages":                  [HumanMessage(content=user_message)],
                    "session_id":                session_id,
                    "user_id":                   user.user_id,
                    "current_message_llm_calls": [],
                    "requires_ticket":           False,
                    "search_results":            None,
                    "user_intent":               None,
                    "search_queries":            None,
                    "app_identified":            None,
                    "health_data":               None,
                    "conversation_summary":      None,
                    "retrieved_memory":          None,
                }

                async for event in master_graph.graph.astream_events(
                    input=initial_input,
                    config=config,
                    version="v2",
                ):
                    event_name = event.get("event", "")
                    metadata   = event.get("metadata", {})
                    node_name  = metadata.get("langgraph_node", "")

                    # LLM token streaming
                    if event_name == "on_chat_model_stream":
                        chunk = event.get("data", {}).get("chunk")
                        if chunk and hasattr(chunk, "content") and chunk.content:
                            token = chunk.content
                            full_response.append(token)
                            await event_queue.put(_fmt("token", {"token": token}))

                    # Node started — agent thinking
                    elif event_name == "on_chain_start" and node_name:
                        status_map = {
                            "agent": "Thinking...",
                            "tools": "Executing tool...",
                        }
                        status = status_map.get(node_name, f"Processing...")
                        await event_queue.put(
                            _fmt("agent_thinking", {"status": status, "node": node_name})
                        )

                    # Tool called
                    elif event_name == "on_tool_start":
                        tool_name  = event.get("name", "")
                        tool_input = event.get("data", {}).get("input", {})
                        query      = tool_input.get("query", tool_input.get("app_name", ""))
                        await event_queue.put(
                            _fmt("tool_call", {"tool": tool_name, "query": str(query)})
                        )

                    # Tool finished
                    elif event_name == "on_tool_end":
                        tool_name   = event.get("name", "")
                        tool_output = str(event.get("data", {}).get("output", ""))

                        # Extract ServiceNow URL if present
                        nonlocal ticket_url
                        if tool_name == "get_servicenow_link" and "SERVICENOW_LINK:" in tool_output:
                            ticket_url = tool_output.split("SERVICENOW_LINK:")[-1].strip().split("\n")[0]

                        found = "NO_RESULTS_FOUND" not in tool_output
                        await event_queue.put(
                            _fmt("tool_result", {"tool": tool_name, "found": found})
                        )

                    # LLM call completed — collect token usage
                    elif event_name == "on_chat_model_end":
                        output = event.get("data", {}).get("output")
                        if output and hasattr(output, "usage_metadata") and output.usage_metadata:
                            response_meta = getattr(output, "response_metadata", {})
                            model = response_meta.get("model_name", "unknown")
                            llm_calls.append({
                                "agent":         "conversational_support_agent",
                                "node":          node_name or "agent_loop",
                                "model":         model,
                                "input_tokens":  output.usage_metadata.get("input_tokens", 0),
                                "output_tokens": output.usage_metadata.get("output_tokens", 0),
                                "total_tokens":  output.usage_metadata.get("total_tokens", 0),
                            })

                # Signal completion
                await event_queue.put(None)

            except asyncio.CancelledError:
                logger.info("Agent stream cancelled for session: %s", session_id)
                # Do not put to queue — generator itself may be cancelled
                # Just let the finally block clean up

            except Exception as e:
                logger.error("Agent error session=%s: %s", session_id, str(e))
                try:
                    await event_queue.put(
                        _fmt("error", {"message": "An error occurred. Please try again."})
                    )
                    await event_queue.put(None)
                except Exception:
                    pass  # Queue may be closed if generator was cancelled

        # ── Heartbeat task ────────────────────────────────────────────────────
        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(_HEARTBEAT_INTERVAL)
                await event_queue.put(_heartbeat())

        # ── Start tasks ───────────────────────────────────────────────────────
        agent_task     = asyncio.create_task(run_agent())
        heartbeat_task = asyncio.create_task(heartbeat())
        _running_tasks[session_id] = agent_task

        try:
            # ── Yield SSE events ──────────────────────────────────────────────
            while True:
                try:
                    item = await event_queue.get()
                except asyncio.CancelledError:
                    # Client disconnected — stop cleanly
                    logger.info("Client disconnected for session: %s", session_id)
                    break

                if item is None:
                    break

                if item == "CANCELLED":
                    # User clicked stop — send stopped message
                    try:
                        yield _fmt("error", {"message": "Response stopped."})
                    except Exception:
                        pass
                    break

                try:
                    yield item
                except Exception:
                    # Client disconnected mid-stream
                    logger.info("Client disconnected mid-stream: %s", session_id)
                    break

            # ── Post-stream: save message + token usage ───────────────────────
            final_content = "".join(full_response)

            if final_content:
                saved = await conversation.save_assistant_message(
                    conversation_id=session_id,
                    user_id=user.user_id,
                    content=final_content,
                    ticket_url=ticket_url,
                )
                message_id = saved.message_id

                # Save token usage
                if llm_calls:
                    await conversation.record_llm_calls(
                        conversation_id=session_id,
                        message_id=message_id,
                        user_id=user.user_id,
                        llm_calls=llm_calls,
                    )

                # Check summary trigger
                if await conversation.should_generate_summary(session_id):
                    logger.info("Summary trigger reached for session: %s", session_id)

                # Emit done event
                yield _fmt("done", {
                    "message_id": message_id,
                    "ticket_url": ticket_url,
                })

                # Auto-generate title from first user message
                await conversation.generate_title_if_needed(
                    conversation_id=session_id,
                    first_user_message=user_message,
                )

        finally:
            heartbeat_task.cancel()
            agent_task.cancel()
            _running_tasks.pop(session_id, None)
            await asyncio.gather(heartbeat_task, agent_task, return_exceptions=True)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx/proxy buffering
            "Connection":       "keep-alive",
        },
    )


# ── Non-streaming endpoint ───────────────────────────────────────────────────

@router.post("/sync")
async def chat_sync(
    request:      ChatSyncRequest,
    user:         CurrentUser,
    conversation: ConversationSvc,
) -> dict:
    """
    Non-streaming chat endpoint.
    Waits for full agent response and returns it at once.
    Use this for:
      - Testing without SSE client
      - Simple integrations that don't need streaming
      - Postman / curl testing

    Returns:
        {
            "message_id":  str,
            "content":     str,
            "ticket_url":  str | None,
            "session_id":  str
        }
    """
    session_id   = request.session_id
    user_message = request.message.strip()

    # Repair checkpoint before running agent
    await _repair_checkpoint_after_cancel(session_id)

    # Save user message
    await conversation.save_user_message(
        conversation_id=session_id,
        user_id=user.user_id,
        content=user_message,
    )

    config = {"configurable": {"thread_id": session_id}}

    initial_input = {
        "messages":                  [HumanMessage(content=user_message)],
        "session_id":                session_id,
        "user_id":                   user.user_id,
        "current_message_llm_calls": [],
        "requires_ticket":           False,
        "search_results":            None,
        "user_intent":               None,
        "search_queries":            None,
        "app_identified":            None,
        "health_data":               None,
        "conversation_summary":      None,
        "retrieved_memory":          None,
    }

    full_response: list[str] = []
    ticket_url:   str | None = None
    llm_calls:    list[dict] = []

    # Run agent — collect full response
    async for event in master_graph.graph.astream_events(
        input=initial_input,
        config=config,
        version="v2",
    ):
        event_name = event.get("event", "")
        metadata   = event.get("metadata", {})
        node_name  = metadata.get("langgraph_node", "")

        # Collect tokens
        if event_name == "on_chat_model_stream":
            chunk = event.get("data", {}).get("chunk")
            if chunk and hasattr(chunk, "content") and chunk.content:
                full_response.append(chunk.content)

        # Extract ticket URL
        elif event_name == "on_tool_end":
            tool_name   = event.get("name", "")
            tool_output = str(event.get("data", {}).get("output", ""))
            if tool_name == "get_servicenow_link" and "SERVICENOW_LINK:" in tool_output:
                ticket_url = tool_output.split("SERVICENOW_LINK:")[-1].strip().split("\n")[0]

        # Collect token usage
        elif event_name == "on_chat_model_end":
            output = event.get("data", {}).get("output")
            if output and hasattr(output, "usage_metadata") and output.usage_metadata:
                response_meta = getattr(output, "response_metadata", {})
                model = response_meta.get("model_name", "unknown")
                llm_calls.append({
                    "agent":         "conversational_support_agent",
                    "node":          node_name or "agent_loop",
                    "model":         model,
                    "input_tokens":  output.usage_metadata.get("input_tokens", 0),
                    "output_tokens": output.usage_metadata.get("output_tokens", 0),
                    "total_tokens":  output.usage_metadata.get("total_tokens", 0),
                })

    # Save assistant message
    final_content = "".join(full_response)
    saved = await conversation.save_assistant_message(
        conversation_id=session_id,
        user_id=user.user_id,
        content=final_content,
        ticket_url=ticket_url,
    )

    # Save token usage
    if llm_calls:
        await conversation.record_llm_calls(
            conversation_id=session_id,
            message_id=saved.message_id,
            user_id=user.user_id,
            llm_calls=llm_calls,
        )

    # Auto-generate title from first user message
    await conversation.generate_title_if_needed(
        conversation_id=session_id,
        first_user_message=user_message,
    )

    return {
        "message_id": saved.message_id,
        "content":    final_content,
        "ticket_url": ticket_url,
        "session_id": session_id,
    }


# ── Stop endpoint ─────────────────────────────────────────────────────────────

@router.post("/stop")
async def stop_stream(
    request: StopRequest,
    user:    CurrentUser,
) -> dict:
    """Cancels a running agent stream."""
    task = _running_tasks.get(request.session_id)
    if task and not task.done():
        task.cancel()
        logger.info("Stream stopped by %s for session %s", user.user_id, request.session_id)
        return {"status": "stopped", "session_id": request.session_id}
    return {"status": "not_running", "session_id": request.session_id}


# ── Session management ────────────────────────────────────────────────────────

@router.get("/sessions")
async def get_sessions(
    user:         CurrentUser,
    conversation: ConversationSvc,
) -> list:
    sessions = await conversation.get_user_sessions(user.user_id)
    return [s.model_dump() for s in sessions]


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
    await conversation.update_title(conversation_id=conversation_id, title=body.title)
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