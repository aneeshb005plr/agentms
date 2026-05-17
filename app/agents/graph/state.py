# app/agents/graph/state.py
# LangGraph state for NextGenAMS agent engine.
#
# Design decisions:
#   - Extends MessagesState — gives add_messages reducer for free
#   - MVP: Layer 1 (message trimming) + Layer 2 (rolling summary)
#   - Layer 3 (semantic memory) — hook left in, not implemented
#
# app_identified:
#   - NOT extracted from user message — unreliable string matching
#   - NOT used to filter vector chunks — Vector API handles relevance via rerank_score
#   - Derived AFTER vector search — most common app in top chunks
#   - Informational only — used for Phase 2 Dataverse health check hook
#
# Vector search quality gate:
#   - rerank_score < VECTOR_RERANK_SCORE_THRESHOLD → no relevant info found
#   - Agent tells user no info available, suggests ticket if needed
#   - No hallucination, no forced answer
#
# Token tracking:
#   - current_message_llm_calls accumulates one entry per LLM call per turn
#   - operator.add reducer — each node appends, never overwrites
#   - After turn: SSE endpoint saves to token_usage + updates conversation totals
#
# thread_id = conversation_id = LangGraph thread_id

import operator
from typing import Annotated
from langgraph.graph import MessagesState


class NextGenAMSState(MessagesState):
    """
    Shared state across all nodes in NextGenAMS agent graph.

    Inherited from MessagesState:
        messages: Annotated[list[AnyMessage], add_messages]
            — auto-appends, deduplicates by message ID
            — trimmed by message_trimmer before each LLM call

    Agent working state — reset each turn:

        user_intent
            What type of question the agent classified.
            "it_support" | "greeting" | "out_of_scope" | "unclear"
            Determines agent flow — greeting responds directly,
            out_of_scope declines politely, it_support searches vector.

        search_queries
            One or more queries the agent decided to search.
            Agent decides count — simple question = 1 query,
            complex question = multiple queries.
            Never hardcoded — LLM decides.

        search_results
            Raw chunks from Vector API response.
            List of dicts matching Vector API chunk structure:
            {
                "text":       str,
                "score":      float,
                "source_url": str,
                "file_name":  str,
                "metadata": {
                    "application": str,   ← used to derive app_identified
                    "is_general":  bool,
                    "rerank_score": float ← quality gate
                    ...
                }
            }

        app_identified
            Most frequently appearing application in top-ranked chunks.
            Derived AFTER vector search from chunk metadata.
            NOT extracted from user message — unreliable.
            NOT used to filter chunks — Vector API handles relevance.
            Informational only — Phase 2 Dataverse health check hook.
            None if chunks are mixed apps or no clear winner.

        health_data
            Application health status from Dataverse (Phase 2).
            Always None in Phase 1 — hook for seamless integration later.

        requires_ticket
            True when agent decides ServiceNow link is appropriate.
            NOT automatic — agent decides based on context:
            - User already tried troubleshooting steps and still failing
            - User explicitly asks for ticket
            - No information found in vector (suggest gently, not force)

    Token tracking — accumulated across ALL LLM calls in one turn:
        current_message_llm_calls
            List of dicts — one entry per LLM call per node.
            operator.add reducer — each node appends, never overwrites.
            Entry:
            {
                "agent": "conversational_support_agent",
                "node":  "intent_classifier",
                "model": "gpt-4o-mini",
                "input_tokens":  95,
                "output_tokens": 12,
                "total_tokens":  107
            }
            After turn completes:
                → Bulk saved to token_usage collection (audit trail)
                → Conversation token totals updated atomically

    Memory — persists across turns via AsyncMongoDBSaver:
        conversation_summary
            Rolling summary of older messages — Layer 2 memory.
            Generated when message count > SUMMARY_TRIGGER_COUNT.
            Replaces old messages in LLM context — never full history.

    Session context — set once, never changes:
        session_id    UUID = conversation_id in MongoDB = LangGraph thread_id
        user_id       XYZ uid from JWT (e.g. abahuleyan001)

    Future hooks — not implemented in MVP:
        retrieved_memory    Layer 3 semantic cross-session memory
                            Always None in Phase 1
    """

    # ── Agent working state ───────────────────────────────────────────────────
    user_intent:     str | None        # "it_support"|"greeting"|"out_of_scope"|"unclear"
    search_queries:  list[str] | None  # agent decides 1 or N — never hardcoded
    search_results:  list[dict] | None # raw Vector API chunks
    app_identified:  str | None        # derived from chunks after search — informational only
    health_data:     dict | None       # Dataverse health — always None in Phase 1
    requires_ticket: bool              # agent decides — never automatic

    # ── Token tracking ────────────────────────────────────────────────────────
    # operator.add — nodes append their LLM call entry, never overwrite
    current_message_llm_calls: Annotated[list[dict], operator.add]

    # ── Memory ────────────────────────────────────────────────────────────────
    conversation_summary: str | None   # Layer 2 rolling summary

    # ── Session context ───────────────────────────────────────────────────────
    session_id: str                    # UUID = conversation_id = LangGraph thread_id
    user_id:    str                    # XYZ uid from JWT

    # ── Future hooks ──────────────────────────────────────────────────────────
    retrieved_memory: list[str] | None # Layer 3 — always None in Phase 1