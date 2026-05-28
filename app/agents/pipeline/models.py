# app/agents/pipeline/models.py
# Internal data contracts between pipeline stages.
#
# Uses standard dataclasses (not Pydantic) — these are internal domain models,
# not API boundary models. Types are guaranteed by our own code, no external
# input validation needed. Python 3.13+ dataclasses are 3x faster than before.
#
# Rule: Pydantic for API boundaries (FastAPI schemas, external data)
#       dataclass for internal domain models (pipeline state, in-process data)

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PipelineRequest:
    """
    Immutable input from HTTP layer — created once, never modified.
    FastAPI already validated the HTTP request before this is created.
    """
    session_id:   str
    user_message: str
    user_id:      str
    user_name:    str = ""


@dataclass
class PipelineContext:
    """
    Mutable pipeline state — each stage reads and writes to this.
    Replaces 20+ scattered nonlocal variables in the old event_generator().

    One instance per request, lives only for the duration of the pipeline run.
    Never serialized, never crosses a process boundary.
    """
    # ── Input ─────────────────────────────────────────────────────────────────
    request: PipelineRequest

    # ── Classification ────────────────────────────────────────────────────────
    intent:          str  = ""
    should_escalate: bool = False

    # ── Conversation history ──────────────────────────────────────────────────
    history_turns: list[dict[str, str]] = field(default_factory=list)

    # ── Agent execution ───────────────────────────────────────────────────────
    agent_ran:     bool      = False
    full_response: list[str] = field(default_factory=list)
    raw_content:   str       = ""
    final_content: str       = ""

    # ── Tool results ──────────────────────────────────────────────────────────
    collected_sources: list[dict[str, Any]] = field(default_factory=list)
    ticket_url:        str | None           = None
    llm_calls:         list[dict[str, Any]] = field(default_factory=list)

    # ── Outputs ───────────────────────────────────────────────────────────────
    message_id:       str | None = None
    suggestion_list:  list[str]  = field(default_factory=list)
    is_first_message: bool       = False

    # ── Convenience properties ────────────────────────────────────────────────
    @property
    def session_id(self) -> str:
        return self.request.session_id

    @property
    def user_message(self) -> str:
        return self.request.user_message

    @property
    def user_id(self) -> str:
        return self.request.user_id

    @property
    def has_sources(self) -> bool:
        return len(self.collected_sources) > 0

    @property
    def has_ticket(self) -> bool:
        return self.ticket_url is not None

    @property
    def has_content(self) -> bool:
        return bool(self.final_content.strip())


# ── SSE formatting helpers ────────────────────────────────────────────────────
# Plain functions — no class needed, these are pure formatters.

def _fmt(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"

def fmt_token(token: str) -> str:
    return _fmt("token", {"token": token})

def fmt_thinking(status: str, node: str) -> str:
    return _fmt("agent_thinking", {"status": status, "node": node})

def fmt_tool_call(tool: str, query: str) -> str:
    return _fmt("tool_call", {"tool": tool, "query": query})

def fmt_tool_result(tool: str, found: bool) -> str:
    return _fmt("tool_result", {"tool": tool, "found": found})

def fmt_done(ctx: PipelineContext) -> str:
    return _fmt("done", {
        "message_id":  ctx.message_id,
        "ticket_url":  ctx.ticket_url,
        "sources":     ctx.collected_sources,
        "suggestions": ctx.suggestion_list,
    })

def fmt_error(message: str) -> str:
    return _fmt("error", {"message": message})

def fmt_heartbeat() -> str:
    return ": heartbeat\n\n"