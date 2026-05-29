# app/agents/pipeline/classifier.py
# Message pre-classifier — simple gatekeeper, not a complex decision engine.
#
# Design (2026):
#   Single gpt-4o-mini call returns BOTH intent AND app context flag.
#   Combining both decisions into one call saves ~150ms and one API call per message.
#
# Flow:
#   1. Python fast-path — pure greetings (zero LLM cost, zero latency)
#   2. Single LLM call — returns intent + app_present flag
#   3. If SEARCH and no app established → VAGUE (ask which application)
#
# Intent options: SEARCH | TICKET | RESOLVED | CASUAL | VAGUE
# App flag: YES (specific PwC app named anywhere) | NO (generic action only)
#
# Fails open: any error → SEARCH (never blocks a real IT question)

import logging

from langchain_core.messages import HumanMessage, SystemMessage
from app.agents.shared.clients.llm_client import llm_client

logger = logging.getLogger(__name__)

# ── Intent constants ──────────────────────────────────────────────────────────
INTENT_SEARCH   = "SEARCH"
INTENT_TICKET   = "TICKET"
INTENT_RESOLVED = "RESOLVED"
INTENT_CASUAL   = "CASUAL"
INTENT_VAGUE    = "VAGUE"

VALID_INTENTS = {INTENT_SEARCH, INTENT_TICKET, INTENT_RESOLVED, INTENT_CASUAL, INTENT_VAGUE}

# ── Pure greeting fast-path ───────────────────────────────────────────────────
_GREETINGS: frozenset[str] = frozenset({
    "hi", "hello", "hey", "hiya", "howdy",
    "good morning", "good afternoon", "good evening", "good day",
    "hi there", "hello there", "hey there", "greetings",
    "sup", "what's up", "whats up",
})

# ── Combined intent + app context prompt ──────────────────────────────────────
# Single LLM call returns both decisions — saves one API round-trip per message
_SYSTEM = (
    "You are a classifier for a PwC IT support chatbot. "
    "Given the conversation history and latest user message, return TWO values "
    "on separate lines:\n\n"

    "Line 1 — INTENT (one word):\n"
    "  TICKET   — user EXPLICITLY requests a ticket/incident with no new IT problem\n"
    "             (raise a ticket, create incident, escalate this, log a ticket)\n"
    "             NOT TICKET if user describes problem AND mentions ticket → SEARCH\n"
    "             NOT TICKET if frustrated or says not enough or check again → SEARCH\n\n"
    "  RESOLVED — user confirms issue is fixed\n"
    "             (it worked, problem solved, that fixed it, working now)\n\n"
    "  CASUAL   — greeting, thanks, praise, OR any message completely unrelated\n"
    "             to PwC IT support, applications, or systems\n"
    "             Examples: 'hi', 'hello', 'thank you', 'great help',\n"
    "             'who is the PM of USA', 'what is the weather', 'write me a poem',\n"
    "             'what is 2+2', 'who are you', 'what can you do'\n"
    "             NOT CASUAL: any question mentioning a PwC app → SEARCH\n"
    "             NOT CASUAL: thanks but still not working → SEARCH\n\n"
    "  VAGUE    — ONLY if ALL true: no history, no app name, no error, no symptom\n"
    "             (I have an issue, help me, not working — first message only)\n"
    "             NOT VAGUE if ANY history exists → SEARCH\n\n"
    "  SEARCH   — default for everything else\n\n"

    "Line 2 — ENTITY (one word YES or NO):\n"
    "  YES — the message or history contains a specific named entity that can be searched:\n"
    "         any proper noun, named tool, system, service, application, or platform\n"
    "         (e.g. Kayak, Astro, SAP, SharePoint, Concur, Teams, any named system)\n"
    "         Generic IT terms alone do NOT count as named entities:\n"
    "         'timesheet', 'sync', 'login', 'access', 'email' → these are NOT named entities\n"
    "  NO  — message has only generic IT terms with no specific named entity\n\n"

    "Line 3 — PERSONAL_PROBLEM (one word YES or NO):\n"
    "  YES — user is describing a personal IT issue they are currently experiencing\n"
    "        (I cannot login, I am unable to, I have an error, my app is crashing,\n"
    "         I need help with my specific issue, it is not working for me)\n"
    "  NO  — user is asking a general question or seeking information\n"
    "        (How many apps do you support, what can you help with,\n"
    "         what is the process for, who do I contact, how does X work,\n"
    "         follow-up questions, capability questions, general IT guidance)\n\n"

    "Examples:\n"
    "  'I cannot login to Astro' → SEARCH\\nYES\\nYES (Astro=named entity)\n"
    "  'What types of travel can I book with Kayak?' → SEARCH\\nYES\\nNO (Kayak=named entity)\n"
    "  'I have sync time issue' → SEARCH\\nNO\\nYES (sync/time=generic, no named entity)\n"
    "  'I am unable to fill timesheet' → SEARCH\\nNO\\nYES (timesheet=generic)\n"
    "  'I cannot login' → SEARCH\\nNO\\nYES (login=generic, no named entity)\n"
    "  'hi' (no history) → CASUAL\\nNO\\nNO\n"
    "  'Astro steps...' then 'still not working' → SEARCH\\nYES\\nYES\n"
    "  'raise a ticket' (after troubleshooting) → TICKET\\nYES\\nNO\n"
    "  'it worked!' → RESOLVED\\nNO\\nNO\n"
    "  'How many apps do you support?' → SEARCH\\nNO\\nNO\n"
    "  'What is the process for requesting software?' → SEARCH\\nNO\\nNO\n"
    "  'I need the number' (follow-up) → SEARCH\\nNO\\nNO\n\n"

    "Return ONLY three lines: intent word, YES/NO for app, YES/NO for personal problem. "
    "No explanation."
)

_USER_TEMPLATE = (
    "{history}"
    "Latest user message: {message}\n\n"
    "Classification:"
)


async def classify(
    message: str,
    history: list[dict] | None = None,
) -> str:
    """
    Classifies user message into one of five intents.
    Uses a single LLM call to determine both intent and app context.

    Args:
        message: The latest user message.
        history: Recent conversation turns { role, content }.

    Returns:
        One of: SEARCH | TICKET | RESOLVED | CASUAL | VAGUE
    """
    cleaned = message.strip().lower().rstrip("!?.")
    history = history or []

    # ── 1. Pure greeting fast-path — zero LLM cost ───────────────────────────
    if not history and cleaned in _GREETINGS:
        logger.info("Classifier: CASUAL (greeting fast-path) — '%s'", message[:40])
        return INTENT_CASUAL

    try:
        # ── 2. Single LLM call — intent + app context ─────────────────────────
        history_str = ""
        if history:
            history_str = "Recent conversation:\n"
            for turn in history[-4:]:
                role    = "User" if turn.get("role") == "user" else "Assistant"
                content = str(turn.get("content", ""))[:200]
                history_str += f"{role}: {content}\n"
            history_str += "\n"

        response = await llm_client.fast.ainvoke([
            SystemMessage(content=_SYSTEM),
            HumanMessage(content=_USER_TEMPLATE.format(
                history=history_str,
                message=message,
            )),
        ])

        lines       = [l.strip().upper() for l in response.content.strip().split("\n") if l.strip()]
        intent      = lines[0] if lines else INTENT_SEARCH
        entity_yn   = lines[1] if len(lines) > 1 else "YES"  # fail open
        personal_yn = lines[2] if len(lines) > 2 else "YES"  # fail open

        if intent not in VALID_INTENTS:
            logger.warning(
                "Classifier returned unknown intent '%s' — defaulting to SEARCH",
                intent,
            )
            intent = INTENT_SEARCH

        # ── 3. Entity + personal problem gate ─────────────────────────────────
        # VAGUE only when BOTH conditions are true:
        #   - No specific named entity anywhere (entity=NO)
        #     Named entity = any proper noun: app name, system, tool, service
        #     Generic terms alone (timesheet, sync, login) do NOT count
        #   - User describing a personal IT problem (personal=YES)
        #
        # "I have sync time issue" → entity=NO, personal=YES → VAGUE ✅
        # "I cannot fill timesheet" → entity=NO, personal=YES → VAGUE ✅
        # "What types of travel in Kayak?" → entity=YES (Kayak) → SEARCH ✅
        # "I cannot login to Astro" → entity=YES (Astro) → SEARCH ✅
        # "How many apps do you support?" → entity=NO, personal=NO → SEARCH ✅
        if intent == INTENT_SEARCH and entity_yn == "NO" and personal_yn == "YES":
            logger.info(
                "Classifier: VAGUE (personal problem, no named entity) — '%s'",
                message[:60],
            )
            return INTENT_VAGUE

        logger.info(
            "Classifier: %s (entity:%s personal:%s) — '%s'%s",
            intent,
            entity_yn,
            personal_yn,
            message[:60],
            f" (with {len(history)} history turns)" if history else "",
        )
        return intent

    except Exception as e:
        logger.warning(
            "Classifier failed: %s — defaulting to SEARCH (fail open)", str(e)
        )
        return INTENT_SEARCH