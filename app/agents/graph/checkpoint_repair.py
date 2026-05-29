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
# Why aupdate_state does NOT work:
#   LangGraph MessagesState uses an append reducer — aupdate_state MERGES messages,
#   it does not replace them. The dangling AIMessage is never actually removed.
#   Verified from LangGraph forum (Sep 2025) and langgraph-checkpoint-mongodb docs.
#
# Production fix — direct MongoDB deletion:
#   The checkpoint_writes collection stores individual node writes including
#   the dangling AIMessage. Deleting the write records for the broken checkpoint
#   forces LangGraph to reconstruct state without the dangling message.
#
#   Steps:
#     1. Read current checkpoint via aget_state (gets checkpoint_id)
#     2. Detect dangling AIMessage with unmatched tool_calls
#     3. Delete checkpoint_writes entries for that checkpoint_id
#     4. Delete the checkpoint document itself
#     5. LangGraph reconstructs from previous clean checkpoint
#
# Called by: orchestrator.py before every agent invocation.
# Never raises — a repair failure must not block the next message.

import logging

logger = logging.getLogger(__name__)


async def repair_checkpoint(session_id: str) -> None:
    """
    Repairs LangGraph checkpoint after a cancelled stream.
    Uses direct MongoDB deletion — bypasses LangGraph's message reducer.
    Safe to call before every message — no-op if checkpoint is clean.
    Never raises — failures are logged as warnings only.
    """
    try:
        from app.agents.graph.master_graph import master_graph
        from app.db                         import db
        from langchain_core.messages        import AIMessage, ToolMessage

        config = {"configurable": {"thread_id": session_id}}

        # ── Read current checkpoint state ─────────────────────────────────────
        state = await master_graph.graph.aget_state(config)
        if not state or not state.values:
            return

        messages = state.values.get("messages", [])
        if not messages:
            return

        # ── Detect dangling AIMessage ─────────────────────────────────────────
        dangling_found = False
        for i, msg in enumerate(messages):
            if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
                tool_call_ids = {tc["id"] for tc in msg.tool_calls}
                tool_response_ids = {
                    m.tool_call_id
                    for m in messages[i + 1:]
                    if isinstance(m, ToolMessage) and hasattr(m, "tool_call_id")
                }
                if not tool_call_ids.issubset(tool_response_ids):
                    dangling_found = True
                    logger.warning(
                        "Repairing checkpoint for session %s — "
                        "dangling AIMessage with tool_calls at index %d",
                        session_id, i,
                    )
                    break

        if not dangling_found:
            return

        # ── Get checkpoint_id for direct MongoDB deletion ─────────────────────
        checkpoint_id = None
        if hasattr(state, "config") and state.config:
            checkpoint_id = (
                state.config.get("configurable", {}).get("checkpoint_id")
            )

        if not checkpoint_id:
            # Fallback — try reading from checkpoint metadata
            checkpoint_id = getattr(state, "checkpoint_id", None)

        if not checkpoint_id:
            logger.warning(
                "Checkpoint repair: could not get checkpoint_id for session %s "
                "— skipping direct delete, LangGraph may still fail",
                session_id,
            )
            return

        # ── Delete broken checkpoint directly from MongoDB ────────────────────
        # This bypasses the message reducer — guaranteed to remove dangling state
        checkpoints_col      = db.client[db.db_name]["checkpoints"]
        checkpoint_writes_col = db.client[db.db_name]["checkpoint_writes"]

        # Delete checkpoint_writes for this broken checkpoint
        writes_result = await checkpoint_writes_col.delete_many({
            "thread_id":     session_id,
            "checkpoint_id": checkpoint_id,
        })

        # Delete the checkpoint document itself
        cp_result = await checkpoints_col.delete_many({
            "thread_id":     session_id,
            "checkpoint_id": checkpoint_id,
        })

        logger.info(
            "Checkpoint repair complete for session %s — "
            "deleted checkpoint_id=%s "
            "(checkpoint docs: %d, write docs: %d)",
            session_id,
            checkpoint_id,
            cp_result.deleted_count,
            writes_result.deleted_count,
        )

    except Exception as e:
        logger.warning(
            "Checkpoint repair failed for session %s: %s — continuing anyway",
            session_id, str(e),
        )