# app/api/v1/chat.py
# Chat API — HTTP layer only.
#
# Responsibility: SSE orchestration, request/response handling.
# Business logic lives in dedicated pipeline modules:
#   app/agents/pipeline/classifier.py   — message classification
#   app/agents/pipeline/responses.py    — canned responses
#   app/agents/pipeline/suggestions.py  — follow-up suggestions
#   app/agents/graph/checkpoint_repair.py — checkpoint repair
#
# Endpoints:
#   POST   /api/v1/chat/                        — SSE stream
#   POST   /api/v1/chat/sync                    — non-streaming (testing)
#   POST   /api/v1/chat/stop                    — cancel stream
#   GET    /api/v1/chat/sessions                — paginated session list
#   POST   /api/v1/chat/sessions                — create session
#   DELETE /api/v1/chat/sessions/{id}           — soft delete
#   GET    /api/v1/chat/sessions/{id}/messages  — paginated messages
#   PATCH  /api/v1/chat/sessions/{id}/title     — rename
#   POST   /api/v1/chat/messages/{id}/reaction  — thumbs up/down
#
# SSE event format: "event: {type}\ndata: {json}\n\n"
# workers=1 MANDATORY — _running_tasks dict is process-local

import asyncio
import json
import logging
import re
from collections.abc import AsyncGenerator
from datetime import datetime

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage
from pydantic import BaseModel

from app.agents.graph.master_graph            import master_graph
from app.agents.graph.checkpoint_repair       import repair_checkpoint
from app.agents.pipeline                      import classifier, responses, suggestions
from app.agents.pipeline.classifier           import (
    INTENT_SEARCH, INTENT_TICKET, INTENT_RESOLVED, INTENT_CASUAL, INTENT_VAGUE,
)
from app.domains.auth.dependencies            import CurrentUser
from app.domains.conversations.schemas        import MessageReactionUpdate
from app.dependencies                         import ConversationSvc
from app.exceptions                           import NotFoundError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["Chat"])

# session_id → asyncio.Task — workers=1 MANDATORY
_running_tasks: dict[str, asyncio.Task] = {}
_HEARTBEAT_INTERVAL = 15  # seconds


# ── Request / Response schemas ────────────────────────────────────────────────

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


# ── SSE helpers ───────────────────────────────────────────────────────────────

def _fmt(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"

def _heartbeat() -> str:
    return ": heartbeat\n\n"


# ── SSE stream endpoint ───────────────────────────────────────────────────────

@router.post("/")
async def chat(
    request:      ChatRequest,
    user:         CurrentUser,
    conversation: ConversationSvc,
) -> StreamingResponse:

    async def event_generator() -> AsyncGenerator[str, None]:
        session_id    = request.session_id
        user_message  = request.message.strip()
        message_id:       str | None  = None
        full_response:    list[str]   = []
        ticket_url:       str | None  = None
        llm_calls:        list[dict]  = []
        collected_sources: list[dict] = []
        event_queue:      asyncio.Queue = asyncio.Queue()

        # ── 1. Repair checkpoint ──────────────────────────────────────────────
        await repair_checkpoint(session_id)

        # ── 2. Save user message ──────────────────────────────────────────────
        await conversation.save_user_message(
            conversation_id=session_id,
            user_id=user.user_id,
            content=user_message,
        )

        # ── 3. Get conversation history for multi-turn intent awareness ─────────
        # Pass recent turns to classifier so "still not working" after troubleshooting
        # is correctly classified as TICKET not VAGUE
        history_result = await conversation.get_messages(conversation_id=session_id)
        history_turns  = [
            {"role": m.role, "content": m.content}
            for m in (history_result.messages if history_result else [])[-4:]  # last 4 turns
            if m.role in ("user", "assistant") and m.content
        ]

        # ── 4. Classify intent ────────────────────────────────────────────────
        intent = await classifier.classify(
            message=user_message,
            history=history_turns,
        )

        # Track whether agent actually ran — safety net only fires if agent ran
        agent_ran = False

        # ── 5. Route by intent ────────────────────────────────────────────────

        # CASUAL / VAGUE / RESOLVED — fast LLM, no agent, no vector search
        if intent in (INTENT_CASUAL, INTENT_VAGUE, INTENT_RESOLVED):
            response_text = await responses.respond(
                intent=intent,
                message=user_message,
                history=history_turns,
            )
            saved = await conversation.save_assistant_message(
                conversation_id=session_id,
                user_id=user.user_id,
                content=response_text,
            )
            yield _fmt("token", {"token": response_text})
            yield _fmt("done", {
                "message_id":  saved.message_id,
                "ticket_url":  None,
                "sources":     [],
                "suggestions": [],
            })
            return

        # TICKET — user wants to escalate, call ticket tool directly (no agent, no search)
        # MCP-ready: when ServiceNow MCP is available, replace get_servicenow_link()
        # with mcp_create_ticket() here — classifier and responses.py stay unchanged
        if intent == INTENT_TICKET:
            from app.agents.tools.ticket_tool import get_servicenow_link
            ticket_result = await get_servicenow_link.ainvoke({})
            extracted_url: str | None = None
            if ticket_result and "SERVICENOW_LINK:" in ticket_result:
                import re as _re
                url_match = _re.search(r"https?://[^ ]+", ticket_result)
                if url_match:
                    extracted_url = url_match.group(0).rstrip(".,;:")

            if extracted_url:
                response_text = (
                    "I understand the issue is still not resolved. "
                    "I have provided a support ticket link below — "
                    "please include a description of the issue, "
                    "the steps you have already tried, and any error messages you saw."
                )
            else:
                response_text = (
                    "I understand the issue is still not resolved. "
                    "Please contact your IT support team directly to raise a ticket."
                )

            saved = await conversation.save_assistant_message(
                conversation_id=session_id,
                user_id=user.user_id,
                content=response_text,
                ticket_url=extracted_url,
            )
            yield _fmt("token", {"token": response_text})
            yield _fmt("done", {
                "message_id":  saved.message_id,
                "ticket_url":  extracted_url,
                "sources":     [],
                "suggestions": [],
            })
            return

        # ── SEARCH — check for implicit escalation BEFORE running agent ────────
        # If conversation history shows prior troubleshooting steps were given
        # AND user's message indicates those steps failed → handle as escalation.
        # Pipeline generates response — agent never runs — no data integrity issues.
        # Decision happens BEFORE streaming so response is complete and contextual.
        if history_turns:
            from app.agents.pipeline.responses import (
                needs_escalation,
                generate_escalation_response,
            )
            should_escalate = await needs_escalation(
                message=user_message,
                history=history_turns,
            )

            if should_escalate:
                # Generate contextual empathetic response with gpt-4o-mini
                escalation_text = await generate_escalation_response(
                    message=user_message,
                    history=history_turns,
                )

                # Get ticket URL
                from app.agents.tools.ticket_tool import get_servicenow_link
                ticket_result   = await get_servicenow_link.ainvoke({})
                escalation_url: str | None = None
                if ticket_result and "SERVICENOW_LINK:" in ticket_result:
                    url_match = re.search(r"https?://[^ ]+", ticket_result)
                    if url_match:
                        escalation_url = url_match.group(0).rstrip(".,;:")

                saved = await conversation.save_assistant_message(
                    conversation_id=session_id,
                    user_id=user.user_id,
                    content=escalation_text,
                    ticket_url=escalation_url,
                )
                yield _fmt("token", {"token": escalation_text})
                yield _fmt("done", {
                    "message_id":  saved.message_id,
                    "ticket_url":  escalation_url,
                    "sources":     [],
                    "suggestions": [],  # no suggestions after escalation
                })
                return

        # No escalation needed — pass to agent (search knowledge base + format response)
        agent_ran = True

        # ── 5. Run agent (classification == "search") ─────────────────────────
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
                    input=initial_input, config=config, version="v2",
                ):
                    event_name = event.get("event", "")
                    node_name  = event.get("metadata", {}).get("langgraph_node", "")

                    if event_name == "on_chat_model_stream":
                        chunk = event.get("data", {}).get("chunk")
                        if chunk and hasattr(chunk, "content") and chunk.content:
                            token = chunk.content
                            full_response.append(token)
                            await event_queue.put(_fmt("token", {"token": token}))

                    elif event_name == "on_chain_start" and node_name:
                        status = {"agent": "Thinking...", "tools": "Executing tool..."}.get(
                            node_name, "Processing..."
                        )
                        await event_queue.put(
                            _fmt("agent_thinking", {"status": status, "node": node_name})
                        )

                    elif event_name == "on_tool_start":
                        tool_input = event.get("data", {}).get("input", {})
                        query      = tool_input.get("query", tool_input.get("app_name", ""))
                        await event_queue.put(
                            _fmt("tool_call", {"tool": event.get("name", ""), "query": str(query)})
                        )

                    elif event_name == "on_tool_end":
                        tool_name   = event.get("name", "")
                        raw_output  = event.get("data", {}).get("output", "")
                        tool_output = (
                            raw_output.content
                            if hasattr(raw_output, "content")
                            else str(raw_output)
                        )

                        nonlocal ticket_url
                        if tool_name == "get_servicenow_link" and "SERVICENOW_LINK:" in tool_output:
                            raw_url   = tool_output.split("SERVICENOW_LINK:")[-1].strip()
                            url_match = re.search(r"https?://[^ ]+", raw_url)
                            if url_match:
                                ticket_url = url_match.group(0).rstrip(".,;:")

                        found = "NO_RESULTS_FOUND" not in tool_output
                        await event_queue.put(
                            _fmt("tool_result", {"tool": tool_name, "found": found})
                        )

                        if tool_name == "search_knowledge_base" and "SOURCES_JSON:" in tool_output:
                            try:
                                sources_raw = tool_output.split("SOURCES_JSON:")[-1].split("\n")[0].strip()
                                parsed      = json.loads(sources_raw)
                                seen_urls   = {s.get("source_url", "") for s in collected_sources}
                                for s in parsed:
                                    url = s.get("source_url", "")
                                    if url not in seen_urls:
                                        collected_sources.append({
                                            "file_name":   s.get("file_name", ""),
                                            "source_url":  url,
                                            "application": s.get("application", ""),
                                        })
                                        seen_urls.add(url)
                            except Exception as e:
                                logger.debug("Source extraction failed: %s", str(e))

                    elif event_name == "on_chat_model_end":
                        output = event.get("data", {}).get("output")
                        if output and hasattr(output, "usage_metadata") and output.usage_metadata:
                            model = getattr(output, "response_metadata", {}).get("model_name", "unknown")
                            llm_calls.append({
                                "agent":         "conversational_support_agent",
                                "node":          node_name or "agent_loop",
                                "model":         model,
                                "input_tokens":  output.usage_metadata.get("input_tokens", 0),
                                "output_tokens": output.usage_metadata.get("output_tokens", 0),
                                "total_tokens":  output.usage_metadata.get("total_tokens", 0),
                            })

                await event_queue.put(None)

            except asyncio.CancelledError:
                logger.info("Agent stream cancelled for session: %s", session_id)

            except Exception as e:
                logger.error("Agent error session=%s: %s", session_id, str(e))
                try:
                    await event_queue.put(_fmt("error", {"message": "An error occurred. Please try again."}))
                    await event_queue.put(None)
                except Exception:
                    pass

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(_HEARTBEAT_INTERVAL)
                await event_queue.put(_heartbeat())

        agent_task     = asyncio.create_task(run_agent())
        heartbeat_task = asyncio.create_task(heartbeat())
        _running_tasks[session_id] = agent_task

        try:
            while True:
                try:
                    item = await event_queue.get()
                except asyncio.CancelledError:
                    logger.info("Client disconnected for session: %s", session_id)
                    break

                if item is None:
                    break

                try:
                    yield item
                except Exception:
                    logger.info("Client disconnected mid-stream: %s", session_id)
                    break

            # ── 6. Post-stream: save + suggestions + done event ───────────────
            final_content = "".join(full_response)

            if final_content:
                # ── Pipeline ticket safety net ────────────────────────────────
                # ONLY fires when: agent actually ran (SEARCH intent) AND
                # found nothing (collected_sources empty) AND
                # agent did not call get_servicenow_link (ticket_url still null).
                # Guards against: agent hallucinating ticket phrase without tool call.
                # Does NOT fire for CASUAL/VAGUE/RESOLVED — those never set agent_ran.
                no_sources    = len(collected_sources) == 0
                no_ticket_yet = ticket_url is None
                if agent_ran and no_sources and no_ticket_yet:
                    try:
                        from app.agents.tools.ticket_tool import get_servicenow_link
                        ticket_result = await get_servicenow_link.ainvoke({})
                        if ticket_result and "SERVICENOW_LINK:" in ticket_result:
                            url_match = re.search(r"https?://[^ ]+", ticket_result)
                            if url_match:
                                ticket_url = url_match.group(0).rstrip(".,;:")
                                logger.info(
                                    "Pipeline ticket safety net — auto-provided ticket URL "
                                    "for session %s (agent found no KB answer)",
                                    session_id,
                                )
                    except Exception as e:
                        logger.warning("Pipeline ticket safety net failed: %s", str(e))

                saved = await conversation.save_assistant_message(
                    conversation_id=session_id,
                    user_id=user.user_id,
                    content=final_content,
                    ticket_url=ticket_url,
                    sources=collected_sources if collected_sources else None,
                )
                message_id = saved.message_id

                if llm_calls:
                    await conversation.record_llm_calls(
                        conversation_id=session_id,
                        message_id=message_id,
                        user_id=user.user_id,
                        llm_calls=llm_calls,
                    )

                if await conversation.should_generate_summary(session_id):
                    logger.info("Summary trigger reached for session: %s", session_id)

                # Suppress suggestions when ticket was offered —
                # user has reached the end of the help flow.
                # Suggestions after a ticket are confusing and irrelevant.
                if ticket_url:
                    suggestion_list = []
                else:
                    suggestion_list = await suggestions.generate(
                        search_results=collected_sources,
                        answer_text=final_content,
                    )

                yield _fmt("done", {
                    "message_id":  message_id,
                    "ticket_url":  ticket_url,
                    "sources":     collected_sources if collected_sources else [],
                    "suggestions": suggestion_list,
                })

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
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":        "keep-alive",
        },
    )


# ── Non-streaming endpoint ────────────────────────────────────────────────────

@router.post("/sync")
async def chat_sync(
    request:      ChatSyncRequest,
    user:         CurrentUser,
    conversation: ConversationSvc,
) -> dict:
    session_id   = request.session_id
    user_message = request.message.strip()

    await repair_checkpoint(session_id)
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

    full_response: list[str]  = []
    ticket_url:   str | None  = None
    llm_calls:    list[dict]  = []

    async for event in master_graph.graph.astream_events(
        input=initial_input, config=config, version="v2",
    ):
        event_name = event.get("event", "")
        node_name  = event.get("metadata", {}).get("langgraph_node", "")

        if event_name == "on_chat_model_stream":
            chunk = event.get("data", {}).get("chunk")
            if chunk and hasattr(chunk, "content") and chunk.content:
                full_response.append(chunk.content)

        elif event_name == "on_tool_end":
            tool_name   = event.get("name", "")
            raw_output  = event.get("data", {}).get("output", "")
            tool_output = (
                raw_output.content if hasattr(raw_output, "content") else str(raw_output)
            )
            if tool_name == "get_servicenow_link" and "SERVICENOW_LINK:" in tool_output:
                raw_url   = tool_output.split("SERVICENOW_LINK:")[-1].strip()
                url_match = re.search(r"https?://[^ ]+", raw_url)
                if url_match:
                    ticket_url = url_match.group(0).rstrip(".,;:")

        elif event_name == "on_chat_model_end":
            output = event.get("data", {}).get("output")
            if output and hasattr(output, "usage_metadata") and output.usage_metadata:
                model = getattr(output, "response_metadata", {}).get("model_name", "unknown")
                llm_calls.append({
                    "agent":         "conversational_support_agent",
                    "node":          node_name or "agent_loop",
                    "model":         model,
                    "input_tokens":  output.usage_metadata.get("input_tokens", 0),
                    "output_tokens": output.usage_metadata.get("output_tokens", 0),
                    "total_tokens":  output.usage_metadata.get("total_tokens", 0),
                })

    final_content = "".join(full_response)
    saved = await conversation.save_assistant_message(
        conversation_id=session_id,
        user_id=user.user_id,
        content=final_content,
        ticket_url=ticket_url,
    )

    if llm_calls:
        await conversation.record_llm_calls(
            conversation_id=session_id,
            message_id=saved.message_id,
            user_id=user.user_id,
            llm_calls=llm_calls,
        )

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
async def stop_stream(request: StopRequest, user: CurrentUser) -> dict:
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
    before:       str | None = None,
    limit:        int        = 20,
) -> dict:
    return await conversation.get_user_sessions(
        user_id=user.user_id, limit=limit, before=before,
    )


@router.post("/sessions")
async def create_session(user: CurrentUser, conversation: ConversationSvc) -> dict:
    session = await conversation.create_session(user_id=user.user_id)
    return session.model_dump()


@router.delete("/sessions/{conversation_id}")
async def delete_session(
    conversation_id: str,
    user:            CurrentUser,
    conversation:    ConversationSvc,
) -> dict:
    await conversation.delete_session(
        conversation_id=conversation_id, user_id=user.user_id,
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
        conversation_id=conversation_id, before=before_dt,
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
        message_id=message_id, reaction=body.reaction,
    )
    if not result:
        raise NotFoundError("Message", message_id)
    return result.model_dump()