# app/agents/graph/checkpoint_repair.py
# Repairs LangGraph checkpoint after a cancelled mid-tool-call stream.
#
# Problem:
#   When SSE stream is cancelled mid-tool-call, LangGraph checkpoint contains:
#     AIMessage(tool_calls=[...])   ← tool was called
#     # ToolMessage MISSING         ← stream cancelled before result arrived
#
#   Next message → Azure OpenAI returns HTTP 400:
#   "tool_calls must be followed by tool messages responding to each tool_call_id"
#
# Fix:
#   Before every new message, scan checkpoint messages for any AIMessage
#   with tool_calls not followed by matching ToolMessages.
#   Remove the dangling AIMessage and everything after it.
#
# Called by: app/api/v1/chat.py — before every agent invocation.
# Never raises — a repair failure must not block the next message.

import logging

logger = logging.getLogger(__name__)


async def repair_checkpoint(session_id: str) -> None:
    """
    Repairs LangGraph checkpoint after a cancelled stream.
    Safe to call before every message — no-op if checkpoint is clean.
    Never raises — failures are logged as warnings only.
    """
    try:
        from app.agents.graph.master_graph import master_graph
        from langchain_core.messages import AIMessage, ToolMessage

        config = {"configurable": {"thread_id": session_id}}

        state = await master_graph.graph.aget_state(config)
        if not state or not state.values:
            return

        messages = state.values.get("messages", [])
        if not messages:
            return

        cleaned = list(messages)
        i = len(cleaned) - 1

        while i >= 0:
            msg = cleaned[i]

            if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
                tool_call_ids = {tc["id"] for tc in msg.tool_calls}

                tool_response_ids = {
                    m.tool_call_id
                    for m in cleaned[i + 1:]
                    if isinstance(m, ToolMessage) and hasattr(m, "tool_call_id")
                }

                if not tool_call_ids.issubset(tool_response_ids):
                    logger.warning(
                        "Repairing checkpoint for session %s — "
                        "removing dangling AIMessage with tool_calls at index %d",
                        session_id, i,
                    )
                    cleaned = cleaned[:i]
                    break

            i -= 1

        if len(cleaned) != len(messages):
            await master_graph.graph.aupdate_state(
                config,
                {"messages": cleaned},
            )
            logger.info(
                "Checkpoint repaired for session %s — removed %d dangling messages",
                session_id, len(messages) - len(cleaned),
            )

            # Verify repair took effect by re-reading state
            # This forces LangGraph to reload from MongoDB, clearing any in-memory cache
            verified_state = await master_graph.graph.aget_state(config)
            verified_msgs  = verified_state.values.get("messages", []) if verified_state and verified_state.values else []
            logger.info(
                "Checkpoint repair verified for session %s — state now has %d messages",
                session_id, len(verified_msgs),
            )

    except Exception as e:
        logger.warning(
            "Checkpoint repair failed for session %s: %s — continuing anyway",
            session_id, str(e),
        )