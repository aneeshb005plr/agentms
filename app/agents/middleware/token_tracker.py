# app/agents/middleware/token_tracker.py
# TokenTrackerMiddleware — after_model hook.
#
# Runs after EVERY LLM call in the agent loop.
# Extracts token usage from LLM response and appends to state.
#
# LangChain AIMessage has usage_metadata automatically populated:
#   response.usage_metadata = {
#       "input_tokens":  int,
#       "output_tokens": int,
#       "total_tokens":  int,
#   }
#
# Uses operator.add reducer in NextGenAMSState — each after_model call
# appends one entry to current_message_llm_calls without overwriting.
#
# After the full turn completes, SSE endpoint reads
# state["current_message_llm_calls"] and:
#   1. Bulk saves all entries to token_usage collection (audit trail)
#   2. Updates conversation token totals atomically (agent-wise summary)

import logging
from typing import Any

from langchain.agents.middleware import AgentMiddleware

logger = logging.getLogger(__name__)

AGENT_NAME = "conversational_support_agent"


class TokenTrackerMiddleware(AgentMiddleware):
    """
    Extracts token usage after every LLM call and appends to state.
    Implements per-call granular token tracking for audit and analytics.
    """

    def after_model(self, state: dict) -> dict[str, Any] | None:
        """
        Called after every LLM invocation in the agent loop.
        Extracts usage_metadata and appends to current_message_llm_calls.
        """
        messages = state.get("messages", [])
        if not messages:
            return None

        # Last message is the AIMessage just generated
        last_message = messages[-1]
        usage = getattr(last_message, "usage_metadata", None)

        if not usage:
            logger.debug(
                "TokenTracker: no usage_metadata on last message — skipping"
            )
            return None

        # Extract model name from response metadata
        response_metadata = getattr(last_message, "response_metadata", {})
        model = response_metadata.get("model_name", "unknown")

        # Build token usage entry
        entry = {
            "agent": AGENT_NAME,
            "node":  "agent_loop",      # within create_agent loop — no named nodes
            "model": model,
            "input_tokens":  usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "total_tokens":  usage.get("total_tokens", 0),
        }

        logger.debug(
            "TokenTracker: model=%s in=%d out=%d total=%d",
            model,
            entry["input_tokens"],
            entry["output_tokens"],
            entry["total_tokens"],
        )

        # operator.add reducer appends — never overwrites
        return {"current_message_llm_calls": [entry]}