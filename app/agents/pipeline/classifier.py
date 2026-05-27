# app/agents/pipeline/classifier.py
# Intent-based message classifier — runs before the agent on every message.
#
# Production-grade approach (2026):
#   Single gpt-4o-mini call with conversation context.
#   Returns a named intent — handler logic in chat.py decides what to do.
#   Adding a new intent = add to the prompt + add handler in chat.py. Zero other changes.
#
# Intents:
#   SEARCH        — IT question, how-to, guidance → agent runs
#   TICKET        — user wants to escalate, raise a ticket → ticket tool directly
#   RESOLVED      — user confirms issue is fixed → acknowledge positively
#   CASUAL        — greeting, thanks, praise, frustration → fast LLM response
#   VAGUE         — not enough context (first message only) → ask clarifying question
#
# Multi-turn awareness:
#   Conversation history (last 3 turns) is passed to classifier.
#   "Still not working" with prior context → TICKET (not VAGUE).
#   Without history it would be classified incorrectly as VAGUE.
#
# MCP-ready:
#   TICKET intent today → get_servicenow_link() returns URL
#   TICKET intent with MCP → create_servicenow_ticket() creates ticket directly
#   Handler in chat.py swaps the tool call — classifier stays unchanged.
#
# Fails open: classifier error → defaults to SEARCH (never blocks a real IT question)

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ── Intent constants ──────────────────────────────────────────────────────────
# Use these in chat.py handler logic — not raw strings
INTENT_SEARCH   = "SEARCH"
INTENT_TICKET   = "TICKET"
INTENT_RESOLVED = "RESOLVED"
INTENT_CASUAL   = "CASUAL"
INTENT_VAGUE    = "VAGUE"

VALID_INTENTS = {INTENT_SEARCH, INTENT_TICKET, INTENT_RESOLVED, INTENT_CASUAL, INTENT_VAGUE}

# ── Classifier prompt ─────────────────────────────────────────────────────────
_SYSTEM = (
    "You classify messages in a PwC IT support chatbot. "
    "You will be given the recent conversation history and the latest user message. "
    "Classify the latest user message into exactly ONE of these intents:\n\n"

    "SEARCH   — User has an IT question, problem, how-to request, or guidance query "
               "about a PwC application or system. This includes troubleshooting requests, "
               "informational questions, and process guidance.\n"
    "           ALSO classify as SEARCH if the message describes an IT problem AND "
               "mentions wanting a ticket — investigate the problem first.\n"
    "           Examples: 'I cannot login to SAP', 'how do I submit timesheet in Workday', "
               "'what is Astro used for', 'steps to request VPN access', "
               "'I am facing issue in Astro I want to create a ticket', "
               "'SAP is broken can you raise a ticket'\n\n"

    "TICKET   — User ONLY wants to escalate or raise a ticket, with NO new IT problem described. "
               "This intent requires EITHER:\n"
    "             (a) A follow-up message after troubleshooting was already attempted "
               "(conversation history shows prior troubleshooting), OR\n"
    "             (b) The message contains ONLY a ticket/escalation request with no new problem.\n"
    "           IMPORTANT: If the message describes an IT problem AND mentions a ticket, "
               "classify as SEARCH — the agent should investigate first.\n"
    "           TICKET examples (follow-up only): 'still not working', 'issue persists', "
               "'nothing worked', 'issue is still there', 'please raise a ticket now'\n"
    "           NOT TICKET (classify as SEARCH instead): "
               "'I am facing issue in Astro I want to create a ticket', "
               "'SAP login broken can you raise a ticket', "
               "'I have a problem with Workday please raise a support request'\n\n"

    "RESOLVED — User confirms their issue is fixed or no longer needs help.\n"
    "           Examples: 'it worked', 'problem solved', 'thanks it is working now', "
               "'issue resolved', 'that fixed it'\n\n"

    "CASUAL   — Greeting, thanks, praise, emotional response, or casual message "
               "with no IT support request.\n"
    "           Examples: 'hi', 'hello', 'thanks', 'thank you so much', "
               "'you are amazing', 'great help', 'good morning'\n\n"

    "VAGUE    — Message is too vague to understand what the user needs and there is "
               "NO prior conversation context to infer from. "
               "IMPORTANT: Only classify as VAGUE if this is the first message AND "
               "it has no application name, no error, no symptom, and no action. "
               "If there is prior conversation history, classify as TICKET or SEARCH instead.\n"
    "           Examples (first message only): 'I have an issue', 'something is wrong', "
               "'I need help', 'not working'\n\n"

    "Answer with ONLY the intent word — no explanation, no punctuation."
)

_USER_TEMPLATE = (
    "{history}"
    "Latest user message: {message}\n\n"
    "Intent:"
)


async def classify(
    message:      str,
    history:      list[dict] | None = None,
) -> str:
    """
    Classifies a user message into one of five intents.

    Args:
        message: The latest user message.
        history: Recent conversation turns for multi-turn context.
                 Each dict: { "role": "user"|"assistant", "content": str }
                 Pass last 3-4 turns for best accuracy.

    Returns:
        One of: SEARCH | TICKET | RESOLVED | CASUAL | VAGUE
    """
    try:
        from app.agents.clients.llm_client import llm_client
        from langchain_core.messages import HumanMessage, SystemMessage

        # Build conversation context string
        history_str = ""
        if history:
            history_str = "Recent conversation:\n"
            for turn in history[-4:]:  # last 4 turns max — enough context, not too many tokens
                role    = "User" if turn.get("role") == "user" else "Assistant"
                content = str(turn.get("content", ""))[:200]  # truncate long messages
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
                "Classifier returned unknown intent '%s' for message '%s' — defaulting to SEARCH",
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
        return INTENT_SEARCH  # fail open — never block a real IT question