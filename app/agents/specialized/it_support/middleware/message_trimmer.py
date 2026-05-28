# app/agents/specialized/it_support/middleware/message_trimmer.py
# MessageTrimmerMiddleware — before_model hook.
#
# Runs before EVERY LLM call in the agent loop.
# Two responsibilities:
#   1. Trim messages to MAX_MESSAGES_IN_CONTEXT (Layer 1 memory)
#   2. Prepend conversation_summary if available (Layer 2 memory)

import logging
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage
from langchain_core.messages.utils import count_tokens_approximately, trim_messages

from app.config import settings

logger = logging.getLogger(__name__)

_MAX_TOKENS = settings.MAX_MESSAGES_IN_CONTEXT * 500


class MessageTrimmerMiddleware(AgentMiddleware):
    """
    Trims message history and injects conversation summary before every LLM call.
    Implements Layer 1 + Layer 2 memory management.
    """

    def before_model(self, state: dict) -> dict[str, Any] | None:
        """
        Called before every LLM invocation in the agent loop.
        Returns state update with trimmed + enriched messages.
        """
        messages = state.get("messages", [])
        summary  = state.get("conversation_summary")

        if not messages:
            return None

        # Step 1 — Trim to token budget
        trimmed = trim_messages(
            messages,
            strategy="last",
            token_counter=count_tokens_approximately,
            max_tokens=_MAX_TOKENS,
            start_on="human",
            allow_partial=False,
        )

        # Step 2 — Prepend summary if available (Layer 2 memory)
        if summary:
            summary_message = SystemMessage(
                content=(
                    f"Summary of earlier conversation:\n{summary}\n\n"
                    "The above is a summary of what was discussed before the "
                    "current messages. Use it as context for follow-up questions."
                )
            )
            trimmed = [summary_message] + trimmed

        original_count = len(messages)
        trimmed_count  = len(trimmed)

        if original_count != trimmed_count:
            logger.debug(
                "Messages trimmed: %d → %d (summary=%s)",
                original_count, trimmed_count, "yes" if summary else "no",
            )

        return {"messages": trimmed}