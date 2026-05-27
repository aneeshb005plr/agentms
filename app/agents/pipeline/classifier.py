# app/agents/pipeline/classifier.py
# Message pre-classifier — simple gatekeeper, not a complex decision engine.
#
# Design philosophy (2026):
#   The classifier only decides what it can decide with HIGH CONFIDENCE
#   from the message alone. Ambiguous cases always go to SEARCH.
#   The agent has full conversation history and is better equipped to
#   reason about complex follow-ups than the classifier.
#
# Four simple binary decisions (in order):
#   1. Is this explicitly requesting a ticket? → TICKET
#   2. Is this confirming resolution? → RESOLVED
#   3. Is this pure casual with zero IT content? → CASUAL
#   4. Is this the very first message with no context at all? → VAGUE
#   5. Everything else → SEARCH (agent handles with full context)
#
# Key principle — SEARCH is the default for ALL ambiguous cases:
#   "not enough info" → SEARCH (agent finds more)
#   "check again" → SEARCH (agent retries)
#   "still not working" → SEARCH (agent sees history, offers ticket naturally)
#   "this is not helpful" → SEARCH (agent finds better answer)
#   Any follow-up message → SEARCH (agent has conversation context)
#
# TICKET intent is ONLY for explicit ticket requests — not inferred frustration.
# The agent + pipeline safety net handle implicit ticket scenarios.
#
# MCP-ready:
#   TICKET today → get_servicenow_link() returns URL
#   TICKET with MCP → create_servicenow_ticket() creates ticket directly
#   Classifier unchanged — only chat.py TICKET handler updates.
#
# Fails open: classifier error → SEARCH (never blocks a real IT question)

import logging

logger = logging.getLogger(__name__)

# ── Intent constants ──────────────────────────────────────────────────────────
INTENT_SEARCH   = "SEARCH"
INTENT_TICKET   = "TICKET"
INTENT_RESOLVED = "RESOLVED"
INTENT_CASUAL   = "CASUAL"
INTENT_VAGUE    = "VAGUE"

VALID_INTENTS = {INTENT_SEARCH, INTENT_TICKET, INTENT_RESOLVED, INTENT_CASUAL, INTENT_VAGUE}

# ── Classifier prompt ─────────────────────────────────────────────────────────
_SYSTEM = (
    "You are a message classifier for a PwC IT support chatbot. "
    "You will be given recent conversation history and the latest user message. "
    "Make FOUR simple binary decisions in this exact order:\n\n"

    "DECISION 1 — Is the user EXPLICITLY requesting a support ticket?\n"
    "   TICKET if the message clearly and explicitly uses words like:\n"
    "   'raise a ticket', 'create a ticket', 'create an incident', 'raise an incident',\n"
    "   'escalate this', 'open a support request', 'log a ticket'\n"
    "   AND the message contains NO new IT problem to investigate.\n"
    "   NOT TICKET if: user describes a problem AND mentions a ticket → classify as SEARCH.\n"
    "   NOT TICKET if: user is frustrated or says 'not enough' or 'check again' → SEARCH.\n"
    "   NOT TICKET if: user says 'still not working' without explicitly requesting a ticket → SEARCH.\n\n"

    "DECISION 2 — Is the user CONFIRMING their issue is RESOLVED?\n"
    "   RESOLVED if the message clearly confirms the issue is fixed or help no longer needed.\n"
    "   Examples: 'it worked', 'problem solved', 'issue resolved', 'that fixed it',\n"
    "             'working now', 'thank you it works'\n\n"

    "DECISION 3 — Is this PURELY casual with ZERO IT content?\n"
    "   CASUAL ONLY if the message is purely a greeting, thanks, or praise\n"
    "   with absolutely no IT question, problem, or request embedded.\n"
    "   Examples: 'hi', 'hello', 'good morning', 'thank you', 'great help'\n"
    "   NOT CASUAL: 'thanks but still not working' → SEARCH\n"
    "   NOT CASUAL: 'ok but can you check again' → SEARCH\n\n"

    "DECISION 4 — Is this the VERY FIRST message with NO context at all?\n"
    "   VAGUE ONLY if ALL of these are true:\n"
    "     - There is NO conversation history shown\n"
    "     - The message has no application name\n"
    "     - The message has no error, symptom, or specific action\n"
    "     - The message is completely unclear what help is needed\n"
    "   Examples (first message only): 'I have an issue', 'help me', 'not working'\n"
    "   NOT VAGUE if there is ANY conversation history → use SEARCH instead.\n\n"

    "DEFAULT — If none of the above apply: SEARCH\n"
    "   SEARCH for ALL of these and more:\n"
    "   - Any IT question, problem, how-to, or guidance request\n"
    "   - User says 'not enough', 'check again', 'give me more', 'try again'\n"
    "   - User is frustrated but hasn't explicitly requested a ticket\n"
    "   - Any follow-up message when conversation history exists\n"
    "   - Anything ambiguous — when in doubt, SEARCH\n\n"

    "Answer with ONLY the intent word. No explanation. No punctuation."
)

_USER_TEMPLATE = (
    "{history}"
    "Latest user message: {message}\n\n"
    "Intent:"
)


async def classify(
    message: str,
    history: list[dict] | None = None,
) -> str:
    """
    Classifies message into one of five intents using simple binary decisions.

    The classifier is a lightweight gatekeeper — not a complex decision engine.
    Ambiguous cases default to SEARCH so the agent can handle them with
    full conversation context.

    Args:
        message: The latest user message.
        history: Recent conversation turns { role, content }.
                 Pass last 4 turns for multi-turn accuracy.

    Returns:
        One of: SEARCH | TICKET | RESOLVED | CASUAL | VAGUE
    """
    try:
        from app.agents.clients.llm_client import llm_client
        from langchain_core.messages import HumanMessage, SystemMessage

        # Build conversation context
        history_str = ""
        if history:
            history_str = "Recent conversation:\n"
            for turn in history[-4:]:
                role    = "User" if turn.get("role") == "user" else "Assistant"
                content = str(turn.get("content", ""))[:200]
                history_str += f"{role}: {content}\n"
            history_str += "\n"

        user_text = _USER_TEMPLATE.format(
            history=history_str,
            message=message,
        )

        response = await llm_client.fast.ainvoke([
            SystemMessage(content=_SYSTEM),
            HumanMessage(content=user_text),
        ])

        raw    = response.content.strip().upper()
        intent = raw.split()[0] if raw else INTENT_SEARCH

        if intent not in VALID_INTENTS:
            logger.warning(
                "Classifier returned unknown intent '%s' for '%s' — defaulting to SEARCH",
                intent, message[:60],
            )
            return INTENT_SEARCH

        logger.info(
            "Classifier: %s — '%s'%s",
            intent,
            message[:60],
            f" (with {len(history)} history turns)" if history else "",
        )
        return intent

    except Exception as e:
        logger.warning(
            "Classifier failed: %s — defaulting to SEARCH (fail open)", str(e)
        )
        return INTENT_SEARCH