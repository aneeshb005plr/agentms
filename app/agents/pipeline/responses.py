# app/agents/pipeline/responses.py
# Non-search intent handlers — fast LLM responses.
#
# Handles: CASUAL, VAGUE, RESOLVED (via respond())
#          ESCALATION (via generate_escalation_response()) — implicit ticket after failed steps
#
# TICKET intent is handled directly in chat.py (calls get_servicenow_link tool).
#
# MCP note:
#   TICKET/ESCALATION handling will expand when ServiceNow MCP is available:
#   Today  → get_servicenow_link() returns URL button
#   Future → create_servicenow_ticket() creates ticket and returns ticket number
#   These modules stay unchanged — only chat.py handler updates.

import logging

logger = logging.getLogger(__name__)

# ── System prompts ────────────────────────────────────────────────────────────

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

_ESCALATION_SYSTEM = (
    "You are NextGenAMS, a PwC IT support assistant. "
    "The user has tried the troubleshooting steps provided in this conversation "
    "but their issue is still not resolved. "
    "Write a warm, empathetic response that:\n"
    "  1. Acknowledges that the user tried the steps and the issue persists\n"
    "  2. Explains you are escalating to the IT support team\n"
    "  3. Lists what the user should include in their support ticket:\n"
    "     - The steps they tried from this conversation\n"
    "     - Any error messages or codes they saw\n"
    "     - The application name and their device type\n"
    "  4. Ends with EXACTLY this sentence: "
    "\"I have provided a support ticket link below.\"\n\n"
    "Use markdown formatting — bold the ticket fields list. "
    "Be empathetic and professional. 3-5 sentences total before the ticket fields."
)

# ── Escalation detection ──────────────────────────────────────────────────────

_ESCALATION_DETECT_SYSTEM = (
    "You determine if an IT support conversation needs escalation to a support ticket. "
    "Answer ONLY with YES or NO.\n\n"
    "Answer YES if ALL of these are true:\n"
    "  1. The conversation history shows the assistant already provided troubleshooting steps\n"
    "  2. The user's latest message indicates those steps did not work or were not sufficient\n\n"
    "Answer NO if:\n"
    "  - This is the first exchange (no prior troubleshooting steps were given)\n"
    "  - The user is asking a new question unrelated to prior steps\n"
    "  - The user wants more information but has not tried the steps yet\n"
    "  - The message is a greeting, thanks, or casual response"
)


async def needs_escalation(
    message: str,
    history: list[dict],
) -> bool:
    """
    Detects if conversation needs escalation BEFORE running the agent.
    Uses gpt-4o-mini — fast (~150ms), cheap, accurate with conversation history.

    Returns True only when:
    - Prior troubleshooting steps were provided in history
    - User's latest message indicates those steps failed

    Fails closed → returns False (run agent normally, never block unnecessarily).
    """
    if not history:
        return False  # no history = first message = cannot be escalation

    try:
        from app.agents.clients.llm_client import llm_client
        from langchain_core.messages import HumanMessage, SystemMessage

        # Build history context — last 6 turns for full picture
        history_str = "Conversation history:\n"
        for turn in history[-6:]:
            role    = "User" if turn.get("role") == "user" else "Assistant"
            content = str(turn.get("content", ""))[:300]
            history_str += f"{role}: {content}\n"

        user_text = (
            f"{history_str}\n"
            f"Latest user message: {message}\n\n"
            "Does this need escalation? YES or NO:"
        )

        response = await llm_client.fast.ainvoke([
            SystemMessage(content=_ESCALATION_DETECT_SYSTEM),
            HumanMessage(content=user_text),
        ])

        answer = response.content.strip().upper()
        result = answer.startswith("YES")

        logger.info(
            "Escalation detection: %s — '%s'",
            "ESCALATE" if result else "no escalation",
            message[:60],
        )
        return result

    except Exception as e:
        logger.warning("Escalation detection failed: %s — defaulting to False", str(e))
        return False  # fail closed — run agent normally


async def generate_escalation_response(
    message: str,
    history: list[dict],
) -> str:
    """
    Generates a contextual, empathetic escalation response using gpt-4o-mini.
    References what was actually tried in the conversation.
    Always ends with "I have provided a support ticket link below."
    """
    try:
        from app.agents.clients.llm_client import llm_client
        from langchain_core.messages import HumanMessage, SystemMessage

        history_str = "Conversation so far:\n"
        for turn in history[-6:]:
            role    = "User" if turn.get("role") == "user" else "Assistant"
            content = str(turn.get("content", ""))[:300]
            history_str += f"{role}: {content}\n"

        response = await llm_client.fast.ainvoke([
            SystemMessage(content=_ESCALATION_SYSTEM),
            HumanMessage(content=f"{history_str}\nLatest user message: {message}"),
        ])

        return response.content.strip()

    except Exception as e:
        logger.warning("Escalation response generation failed: %s", str(e))
        # Fallback — static but complete
        return (
            "I understand you have tried the troubleshooting steps and the issue persists. "
            "I recommend escalating this to the IT support team for further investigation.\n\n"
            "When submitting your ticket please include:\n"
            "- **Steps tried:** the troubleshooting steps from this conversation\n"
            "- **Error messages:** any codes or messages you saw\n"
            "- **Application and device:** the app name and your device type\n\n"
            "I have provided a support ticket link below."
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
        history: Recent conversation turns for context.

    Returns:
        Response text string.
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

        fallbacks = {
            "CASUAL":   "Hello! How can I assist you with your IT needs today?",
            "VAGUE":    "I'd be happy to help. Could you tell me which application you're asking about and what specifically is happening?",
            "RESOLVED": "Glad to hear it's resolved! Feel free to reach out if you need anything else.",
        }
        return fallbacks[intent]