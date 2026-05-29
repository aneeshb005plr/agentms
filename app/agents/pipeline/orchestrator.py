# app/agents/pipeline/orchestrator.py
# ChatPipeline — sequences all pipeline stages for a single chat request.
#
# Responsibilities:
#   - Owns the SSE event generator
#   - Calls each stage in order
#   - Passes PipelineContext through all stages
#   - Manages asyncio tasks (agent + heartbeat)
#   - Manages _running_tasks for stop streaming
#
# What it does NOT do:
#   - No business logic — each stage module owns its logic
#   - No HTTP concerns — chat.py owns request/response
#   - No database — ConversationSvc injected, never instantiated here
#
# Adding a new pipeline stage:
#   1. Create module in app/agents/pipeline/
#   2. Add one method call in the correct place below
#   3. Zero changes to chat.py or any other stage
#
# Adding a new agent (Phase 2):
#   1. Add domain to classifier
#   2. Add routing in _run_agent() based on ctx.domain
#   3. Zero changes to this file's stage sequence

import asyncio
import json
import logging
import re
from collections.abc import AsyncGenerator

from langchain_core.messages import HumanMessage

from app.agents.graph.checkpoint_repair   import repair_checkpoint
from app.agents.graph.master_graph        import master_graph
from app.agents.pipeline.classifier       import (
    INTENT_CASUAL, INTENT_RESOLVED, INTENT_SEARCH,
    INTENT_TICKET, INTENT_VAGUE,
)
from app.agents.pipeline.models           import (
    PipelineContext, PipelineRequest,
    fmt_done, fmt_error, fmt_heartbeat,
    fmt_thinking, fmt_token, fmt_tool_call, fmt_tool_result,
)
from app.agents.pipeline                  import classifier, formatter, responses, suggestions
from app.agents.pipeline.post_processor   import process as post_process
from app.agents.shared.tools.ticket_tool         import get_servicenow_link

logger = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL = 15  # seconds


def _extract_ticket_url(tool_output: str) -> str | None:
    """Extracts ServiceNow URL from tool output string."""
    if "SERVICENOW_LINK:" not in tool_output:
        return None
    raw = tool_output.split("SERVICENOW_LINK:")[-1].strip()
    match = re.search(r"https?://[^ ]+", raw)
    return match.group(0).rstrip(".,;:") if match else None


async def _get_ticket_url() -> str | None:
    """Calls get_servicenow_link tool and returns URL."""
    try:
        result = await get_servicenow_link.ainvoke({})
        return _extract_ticket_url(result or "")
    except Exception as e:
        logger.warning("get_servicenow_link failed: %s", str(e))
        return None


class ChatPipeline:
    """
    Sequences all pipeline stages for a single chat request.
    One instance per request — not a singleton.
    """

    def __init__(self, conversation_svc) -> None:
        # ConversationSvc injected from FastAPI dependency — not created here
        self._conv = conversation_svc

    async def run(
        self,
        request:       PipelineRequest,
        running_tasks: dict[str, asyncio.Task],
    ) -> AsyncGenerator[str, None]:
        """
        Main entry point — returns an async generator of SSE strings.
        Called by chat.py StreamingResponse.

        Args:
            request:       Validated pipeline request from HTTP layer.
            running_tasks: Shared dict for stop-stream task tracking.
                           Orchestrator registers agent task here so
                           stop endpoint can cancel it.
        """
        ctx = PipelineContext(request=request)
        async for event in self._event_generator(ctx, running_tasks):
            yield event

    async def _event_generator(
        self,
        ctx:           PipelineContext,
        running_tasks: dict[str, asyncio.Task],
    ) -> AsyncGenerator[str, None]:

        # ── Stage 1: Repair checkpoint ────────────────────────────────────────
        await repair_checkpoint(ctx.session_id)

        # ── Stage 2: Load conversation history BEFORE saving user message ───────
        # CRITICAL: must load BEFORE save_user_message() otherwise the current
        # message is included in history and classifier sees it as "has history"
        # causing greetings and casual messages to bypass VAGUE detection → SEARCH
        history_result = await self._conv.get_messages(conversation_id=ctx.session_id)
        ctx.history_turns = [
            {"role": m.role, "content": m.content}
            for m in (history_result.messages if history_result else [])[-4:]
            if m.role in ("user", "assistant") and m.content
        ]

        # ── Stage 3: Save user message ────────────────────────────────────────
        await self._conv.save_user_message(
            conversation_id=ctx.session_id,
            user_id=ctx.user_id,
            content=ctx.user_message,
        )

        # ── Stage 4: Classify intent ──────────────────────────────────────────
        ctx.intent = await classifier.classify(
            message=ctx.user_message,
            history=ctx.history_turns,
        )

        # ── Stage 5: Route by intent ──────────────────────────────────────────
        async for event in self._route(ctx, running_tasks):
            yield event

    # ── Intent routing ────────────────────────────────────────────────────────

    async def _route(
        self,
        ctx:           PipelineContext,
        running_tasks: dict[str, asyncio.Task],
    ) -> AsyncGenerator[str, None]:
        """Routes to the correct handler based on classified intent."""

        if ctx.intent in (INTENT_CASUAL, INTENT_VAGUE, INTENT_RESOLVED):
            async for event in self._handle_conversational(ctx):
                yield event

        elif ctx.intent == INTENT_TICKET:
            async for event in self._handle_ticket(ctx):
                yield event

        else:  # INTENT_SEARCH
            async for event in self._handle_search(ctx, running_tasks):
                yield event

    # ── Conversational handler (CASUAL / VAGUE / RESOLVED) ────────────────────

    async def _handle_conversational(self, ctx: PipelineContext) -> AsyncGenerator[str, None]:
        """Fast LLM response — no agent, no vector search."""
        response_text = await responses.respond(
            intent=ctx.intent,
            message=ctx.user_message,
            history=ctx.history_turns,
        )
        ctx.final_content = response_text
        saved = await self._conv.save_assistant_message(
            conversation_id=ctx.session_id,
            user_id=ctx.user_id,
            content=response_text,
        )
        ctx.message_id = saved.message_id
        yield fmt_token(response_text)
        yield fmt_done(ctx)

    # ── Explicit ticket handler (TICKET) ──────────────────────────────────────

    async def _handle_ticket(self, ctx: PipelineContext) -> AsyncGenerator[str, None]:
        """
        User explicitly requested a ticket.
        Generates contextual response based on conversation history.
        MCP-ready: replace _get_ticket_url() with MCP create_ticket() when available.
        """
        # Get ticket URL and generate contextual response in parallel
        ticket_url_task  = asyncio.create_task(_get_ticket_url())
        response_task    = asyncio.create_task(
            responses.generate_ticket_response(
                message=ctx.user_message,
                history=ctx.history_turns,
            )
        )
        ctx.ticket_url, response_text = await asyncio.gather(
            ticket_url_task, response_task
        )

        if not ctx.ticket_url:
            response_text = (
                "Please contact your IT support team directly to raise a ticket."
            )

        ctx.final_content = response_text
        saved = await self._conv.save_assistant_message(
            conversation_id=ctx.session_id,
            user_id=ctx.user_id,
            content=response_text,
            ticket_url=ctx.ticket_url,
        )
        ctx.message_id = saved.message_id
        yield fmt_token(response_text)
        yield fmt_done(ctx)

    # ── Search handler (SEARCH) ───────────────────────────────────────────────

    async def _handle_search(
        self,
        ctx:           PipelineContext,
        running_tasks: dict[str, asyncio.Task],
    ) -> AsyncGenerator[str, None]:
        """
        Full search pipeline:
          escalation check → agent → format → post-process → save → suggestions
        """
        # ── Escalation check (before agent) ──────────────────────────────────
        if ctx.history_turns:
            should_escalate = await responses.needs_escalation(
                message=ctx.user_message,
                history=ctx.history_turns,
            )
            if should_escalate:
                async for event in self._handle_escalation(ctx):
                    yield event
                return

        # ── Run agent ─────────────────────────────────────────────────────────
        ctx.agent_ran = True
        event_queue: asyncio.Queue = asyncio.Queue()

        # Inject conversation context into user message before passing to agent
        # This ensures the agent uses full context when building search queries
        # e.g. "in Astro" after "time sync issue" → agent knows to search "Astro time sync"
        ctx = self._inject_context(ctx)

        agent_task     = asyncio.create_task(self._run_agent(ctx, event_queue))
        heartbeat_task = asyncio.create_task(self._heartbeat(event_queue))

        # Register agent task for stop-stream support
        # chat.py passes _running_tasks dict — stop endpoint cancels via this
        running_tasks[ctx.session_id] = agent_task

        try:
            # Stream thinking/tool events to user while agent runs
            while True:
                try:
                    item = await event_queue.get()
                except asyncio.CancelledError:
                    logger.info("Client disconnected: %s", ctx.session_id)
                    break

                if item is None:
                    break

                try:
                    yield item
                except Exception:
                    logger.info("Client disconnected mid-stream: %s", ctx.session_id)
                    break

            # ── Post-agent: format → post-process → stream ────────────────────
            ctx.raw_content   = "".join(ctx.full_response)
            ctx.final_content = ctx.raw_content

            if ctx.raw_content:
                # Format with markdown
                ctx.final_content = await formatter.format_response(ctx.raw_content)

                # Post-process (strip orphaned sentences, duplicate links)
                ctx.final_content = post_process(
                    content=ctx.final_content,
                    ticket_url=ctx.ticket_url,
                    session_id=ctx.session_id,
                )

                # Stream formatted content in chunks
                chunk_size = 8
                for i in range(0, len(ctx.final_content), chunk_size):
                    yield fmt_token(ctx.final_content[i:i + chunk_size])

            # ── Save + suggestions + title + done ────────────────────────────
            if ctx.has_content:
                await self._persist(ctx)

                # Generate title BEFORE done event so frontend gets it immediately
                # Title included in done payload — no setTimeout reload needed
                ctx.title = await self._conv.generate_title_if_needed(
                    conversation_id=ctx.session_id,
                    first_user_message=ctx.user_message,
                )

                yield fmt_done(ctx)

        finally:
            heartbeat_task.cancel()
            agent_task.cancel()
            await asyncio.gather(heartbeat_task, agent_task, return_exceptions=True)

    # ── Escalation handler ────────────────────────────────────────────────────

    async def _handle_escalation(self, ctx: PipelineContext) -> AsyncGenerator[str, None]:
        """
        Implicit escalation — prior steps failed.
        Pipeline generates contextual response, no agent runs.
        """
        escalation_text   = await responses.generate_escalation_response(
            message=ctx.user_message,
            history=ctx.history_turns,
        )
        ctx.ticket_url    = await _get_ticket_url()
        ctx.final_content = escalation_text

        saved = await self._conv.save_assistant_message(
            conversation_id=ctx.session_id,
            user_id=ctx.user_id,
            content=escalation_text,
            ticket_url=ctx.ticket_url,
        )
        ctx.message_id = saved.message_id
        yield fmt_token(escalation_text)
        yield fmt_done(ctx)

    # ── Context injection ────────────────────────────────────────────────────

    def _inject_context(self, ctx: PipelineContext) -> PipelineContext:
        """
        Augments the user message with structured conversation context.

        Problem: when user says "in Astro" after "time sync issue",
        the agent only sees "in Astro" and forms a generic query.

        Fix: prepend recent history context to user_message so agent
        always has full context when building search queries.
        Only injects when history has IT-relevant content — not greetings.
        """
        if not ctx.history_turns:
            return ctx

        # Build context from last 2 turns (most recent relevant context)
        it_turns = [
            t for t in ctx.history_turns[-4:]
            if len(str(t.get("content", ""))) > 20  # skip very short messages like "hi"
        ]

        if not it_turns:
            return ctx

        context_lines = []
        for turn in it_turns[-2:]:  # last 2 meaningful turns
            role    = "User" if turn.get("role") == "user" else "Assistant"
            content = str(turn.get("content", ""))[:300]
            context_lines.append(f"{role}: {content}")

        if not context_lines:
            return ctx

        context_prefix = (
            "[Conversation context for search query building:\n"
            + "\n".join(context_lines)
            + "\n]\n\n"
        )

        # Create new context with augmented message
        # PipelineContext.user_message is a property from request — create new request
        from dataclasses import replace
        new_request = replace(
            ctx.request,
            user_message=context_prefix + ctx.user_message,
        )
        return replace(ctx, request=new_request)

    # ── Agent runner ──────────────────────────────────────────────────────────

    async def _run_agent(
        self,
        ctx:         PipelineContext,
        event_queue: asyncio.Queue,
    ) -> None:
        """Runs LangGraph agent, buffers tokens, puts events into queue."""
        try:
            config = {"configurable": {"thread_id": ctx.session_id}}
            initial_input = {
                "messages":                  [HumanMessage(content=ctx.user_message)],
                "session_id":                ctx.session_id,
                "user_id":                   ctx.user_id,
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
                        ctx.full_response.append(chunk.content)
                        # Buffer only — formatter runs after agent completes

                elif event_name == "on_chain_start" and node_name:
                    status = {
                        "agent": "Thinking...",
                        "tools": "Executing tool...",
                    }.get(node_name, "Processing...")
                    await event_queue.put(fmt_thinking(status, node_name))

                elif event_name == "on_tool_start":
                    tool_input = event.get("data", {}).get("input", {})
                    query      = tool_input.get("query", tool_input.get("app_name", ""))
                    await event_queue.put(fmt_tool_call(event.get("name", ""), str(query)))

                elif event_name == "on_tool_end":
                    await self._handle_tool_end(ctx, event, event_queue, node_name)

                elif event_name == "on_chat_model_end":
                    self._handle_model_end(ctx, event, node_name)

            await event_queue.put(None)

        except asyncio.CancelledError:
            logger.info("Agent cancelled: %s", ctx.session_id)

        except Exception as e:
            logger.error("Agent error session=%s: %s", ctx.session_id, str(e))
            try:
                await event_queue.put(fmt_error("An error occurred. Please try again."))
                await event_queue.put(None)
            except Exception:
                pass

    async def _handle_tool_end(
        self,
        ctx:         PipelineContext,
        event:       dict,
        event_queue: asyncio.Queue,
        node_name:   str,
    ) -> None:
        """Processes on_tool_end events — extracts ticket URL and sources."""
        tool_name  = event.get("name", "")
        raw_output = event.get("data", {}).get("output", "")
        tool_output = (
            raw_output.content
            if hasattr(raw_output, "content")
            else str(raw_output)
        )

        # Extract ticket URL if agent called get_servicenow_link
        if tool_name == "get_servicenow_link":
            ctx.ticket_url = _extract_ticket_url(tool_output) or ctx.ticket_url

        # Extract sources from search tool
        if tool_name == "search_knowledge_base" and "SOURCES_JSON:" in tool_output:
            try:
                raw     = tool_output.split("SOURCES_JSON:")[-1].split("\n")[0].strip()
                parsed  = json.loads(raw)
                seen    = {s.get("source_url", "") for s in ctx.collected_sources}
                for s in parsed:
                    url = s.get("source_url", "")
                    if url not in seen:
                        ctx.collected_sources.append({
                            "file_name":   s.get("file_name", ""),
                            "source_url":  url,
                            "application": s.get("application", ""),
                        })
                        seen.add(url)
            except Exception as e:
                logger.debug("Source extraction failed: %s", str(e))

        found = "NO_RESULTS_FOUND" not in tool_output
        await event_queue.put(fmt_tool_result(tool_name, found))

    def _handle_model_end(
        self,
        ctx:       PipelineContext,
        event:     dict,
        node_name: str,
    ) -> None:
        """Tracks token usage from on_chat_model_end events."""
        output = event.get("data", {}).get("output")
        if output and hasattr(output, "usage_metadata") and output.usage_metadata:
            model = getattr(output, "response_metadata", {}).get("model_name", "unknown")
            ctx.llm_calls.append({
                "agent":         "conversational_support_agent",
                "node":          node_name or "agent_loop",
                "model":         model,
                "input_tokens":  output.usage_metadata.get("input_tokens", 0),
                "output_tokens": output.usage_metadata.get("output_tokens", 0),
                "total_tokens":  output.usage_metadata.get("total_tokens", 0),
            })

    # ── Heartbeat ─────────────────────────────────────────────────────────────

    async def _heartbeat(self, event_queue: asyncio.Queue) -> None:
        """Sends heartbeat every 15s — prevents proxy from closing idle SSE connection."""
        while True:
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
            await event_queue.put(fmt_heartbeat())

    # ── Persist + suggestions ─────────────────────────────────────────────────

    async def _persist(self, ctx: PipelineContext) -> None:
        """Saves assistant message, records LLM calls, generates suggestions."""
        saved = await self._conv.save_assistant_message(
            conversation_id=ctx.session_id,
            user_id=ctx.user_id,
            content=ctx.final_content,
            ticket_url=ctx.ticket_url,
            sources=ctx.collected_sources if ctx.collected_sources else None,
        )
        ctx.message_id = saved.message_id

        if ctx.llm_calls:
            await self._conv.record_llm_calls(
                conversation_id=ctx.session_id,
                message_id=ctx.message_id,
                user_id=ctx.user_id,
                llm_calls=ctx.llm_calls,
            )

        if await self._conv.should_generate_summary(ctx.session_id):
            logger.info("Summary trigger reached: %s", ctx.session_id)

        # No suggestions after ticket — user has reached end of help flow
        if ctx.has_ticket:
            ctx.suggestion_list = []
        else:
            ctx.suggestion_list = await suggestions.generate(
                search_results=ctx.collected_sources,
                answer_text=ctx.final_content,
            )