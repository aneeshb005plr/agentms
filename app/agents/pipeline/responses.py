# app/agents/pipeline/responses.py
# Non-search intent handlers — fast LLM responses for CASUAL, VAGUE, RESOLVED.
#
# All three use gpt-4o-mini with focused system prompts.
# No static strings — every response is natural and contextually appropriate.
# TICKET intent is handled directly in chat.py (calls get_servicenow_link tool).
#
# MCP note:
#   TICKET handling will expand when ServiceNow MCP is available:
#   Today  → get_servicenow_link() returns URL button
#   Future → create_servicenow_ticket() creates ticket and returns ticket number
#   The classifier and this module stay unchanged — only chat.py handler updates.

import logging

logger = logging.getLogger(__name__)

# ── System prompts for each non-search intent ─────────────────────────────────

_CASUAL_SYSTEM = (
    "You are NextGenAMS, a friendly PwC IT support assistant. "
    "The user has sent a casual message — a greeting, thanks, praise, or emotional response. "
    "Respond naturally, warmly, and briefly in 1-2 sentences. "
    "If it is a greeting: welcome them and invite them to share their IT issue. "
    "If it is thanks or praise: acknowledge genuinely and offer further help. "
    "If it is frustration or annoyance: acknowledge empathetically and offer to assist. "
    "Never be robotic. Never use bullet points. Keep it human and concise."
)

_VAGUE_SYSTEM = (
    "You are NextGenAMS, a PwC IT support assistant. "
    "The user's message is too vague to search the knowledge base — it lacks specifics. "
    "Ask ONE natural, friendly clarifying question to understand: "
    "  (1) which PwC application or system they are asking about, and "
    "  (2) what specifically is happening — error, failing action, or guidance needed. "
    "Keep it to one sentence. Do not ask multiple questions. Be warm and helpful."
)

_RESOLVED_SYSTEM = (
    "You are NextGenAMS, a PwC IT support assistant. "
    "The user has confirmed their issue is resolved or they no longer need help. "
    "Respond warmly and briefly in 1-2 sentences. "
    "Acknowledge positively and let them know you are available if they need anything else. "
    "Do not offer further troubleshooting — the issue is resolved."
)


async def respond(
    intent:  str,
    message: str,
    history: list[dict] | None = None,
) -> str:
    """
    Generates a natural LLM response for non-search intents.

    Args:
        intent:  One of CASUAL | VAGUE | RESOLVED
        message: The user's message.
        history: Recent conversation turns for context (optional).

    Returns:
        Response text string.

    Raises:
        ValueError for unknown intents (SEARCH and TICKET not handled here).
    """
    system_map = {
        "CASUAL":   _CASUAL_SYSTEM,
        "VAGUE":    _VAGUE_SYSTEM,
        "RESOLVED": _RESOLVED_SYSTEM,
    }

    if intent not in system_map:
        raise ValueError(
            f"pipeline/responses.py does not handle intent '{intent}'. "
            "SEARCH goes to agent, TICKET goes to ticket tool in chat.py."
        )

    system_prompt = system_map[intent]

    try:
        from app.agents.clients.llm_client import llm_client
        from langchain_core.messages import HumanMessage, SystemMessage

        # Build context-aware user message
        context = ""
        if history:
            context = "Recent conversation:\n"
            for turn in history[-3:]:
                role    = "User" if turn.get("role") == "user" else "Assistant"
                content = str(turn.get("content", ""))[:150]
                context += f"{role}: {content}\n"
            context += "\n"

        response = await llm_client.fast.ainvoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=f"{context}User message: {message}"),
        ])

        return response.content.strip()

    except Exception as e:
        logger.warning("pipeline/responses.py LLM call failed: %s", str(e))

        # Fallback static responses — only used if LLM call fails
        fallbacks = {
            "CASUAL":   "Hello! How can I assist you with your IT needs today?",
            "VAGUE":    "I'd be happy to help. Could you tell me which application you're asking about and what specifically is happening?",
            "RESOLVED": "Glad to hear it's resolved! Feel free to reach out if you need anything else.",
        }
        return fallbacks[intent]