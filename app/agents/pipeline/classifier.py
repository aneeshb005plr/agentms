# app/agents/pipeline/classifier.py
# Message pre-classifier — simple gatekeeper, not a complex decision engine.
#
# Design philosophy (2026):
#   Classifier only decides what it can with HIGH CONFIDENCE.
#   Ambiguous cases default to SEARCH — agent has full context.
#
# Flow:
#   1. Python fast-path — pure greetings (zero LLM cost)
#   2. LLM intent classification — 4 binary decisions
#   3. App context check — if SEARCH, verify app is established
#      anywhere in message OR history before sending to agent
#
# App context rule (production-grade):
#   Check BOTH current message AND history for any app name.
#   "hi" + greeting response + "I have time sync issue" → no app anywhere → VAGUE
#   "Astro steps" + "still not working" → Astro in history → SEARCH
#   "I cannot login to Astro" → app in message → SEARCH
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

# ── Intent classifier prompt ──────────────────────────────────────────────────
_INTENT_SYSTEM = (
    "You are a message classifier for a PwC IT support chatbot. "
    "You will be given recent conversation history and the latest user message. "
    "Make FOUR simple binary decisions in this exact order:\n\n"

    "DECISION 1 — Is the user EXPLICITLY requesting a support ticket?\n"
    "   TICKET if message clearly uses: 'raise a ticket', 'create a ticket', "
    "   'create an incident', 'raise an incident', 'escalate this', "
    "   'open a support request', 'log a ticket'\n"
    "   AND the message contains NO new IT problem to investigate.\n"
    "   NOT TICKET: user describes problem AND mentions ticket → SEARCH\n"
    "   NOT TICKET: frustrated or says 'not enough' or 'check again' → SEARCH\n"
    "   NOT TICKET: 'still not working' without explicit ticket request → SEARCH\n\n"

    "DECISION 2 — Is the user CONFIRMING their issue is RESOLVED?\n"
    "   RESOLVED if message confirms issue is fixed or help no longer needed.\n"
    "   Examples: 'it worked', 'problem solved', 'that fixed it', 'working now'\n\n"

    "DECISION 3 — Is this PURELY casual with ZERO IT content?\n"
    "   CASUAL ONLY if purely a greeting, thanks, or praise with no IT request.\n"
    "   Examples: 'hi', 'hello', 'thank you', 'great help'\n"
    "   NOT CASUAL: 'thanks but still not working' → SEARCH\n\n"

    "DECISION 4 — Is this the VERY FIRST message with NO context?\n"
    "   VAGUE ONLY if ALL true: no history, no app name, no error, no symptom.\n"
    "   Examples (first message only): 'I have an issue', 'help me', 'not working'\n"
    "   NOT VAGUE if ANY conversation history exists → SEARCH instead.\n\n"

    "DEFAULT: SEARCH — when in doubt, always SEARCH.\n\n"

    "Answer with ONLY the intent word. No explanation. No punctuation."
)

# ── App context detection prompt ──────────────────────────────────────────────
_APP_CONTEXT_SYSTEM = (
    "You determine if a PwC IT support conversation has established a specific "
    "application or system context. Answer ONLY with YES or NO.\n\n"
    "Answer YES if ANY message in the conversation explicitly names a specific "
    "PwC application or system (e.g. Astro, SAP, Workday, Outlook, Teams, "
    "ServiceNow, SharePoint, Concur, Ariba, Calculo, Kayak, USRPP, Nova, "
    "Interim Time, or any other named tool or platform).\n\n"
    "Answer NO if:\n"
    "  - No application name appears anywhere in the conversation\n"
    "  - Only generic actions are mentioned (fill timesheet, login, access, submit)\n"
    "  - Only greetings or casual messages appear in history\n\n"
    "Examples:\n"
    "  User: hi | Agent: Hello! | User: I have time sync issue → NO\n"
    "  User: I cannot login to Astro → YES\n"
    "  User: Astro steps failed | Agent: try these | User: still not working → YES\n"
    "  User: I am unable to fill timesheet → NO\n"
    "  User: I need help | Agent: which app? | User: Astro → YES\n"
)

_USER_TEMPLATE = (
    "{history}"
    "Latest user message: {message}\n\n"
    "Intent:"
)

_APP_CONTEXT_USER_TEMPLATE = (
    "Conversation:\n{context}\n\n"
    "Does this conversation have a specific application established? YES or NO:"
)


async def _check_app_context(message: str, history: list[dict]) -> bool:
    """
    Checks if a specific PwC application is established ANYWHERE in the
    conversation — current message OR recent history turns.

    This prevents hallucination: "I have time sync issue" after "hi/hello"
    exchange has no app context → VAGUE → ask which application.

    Fails open → True (assume app present, never block a real IT question).
    """
    try:
        # Build full conversation context including current message
        lines: list[str] = []
        for turn in (history or [])[-4:]:
            role    = "User" if turn.get("role") == "user" else "Agent"
            content = str(turn.get("content", ""))[:200]
            lines.append(f"{role}: {content}")
        lines.append(f"User: {message}")
        context = "\n".join(lines)

        response = await llm_client.fast.ainvoke([
            SystemMessage(content=_APP_CONTEXT_SYSTEM),
            HumanMessage(content=_APP_CONTEXT_USER_TEMPLATE.format(context=context)),
        ])

        result = response.content.strip().upper().startswith("YES")
        logger.debug(
            "App context check: %s — message='%s' history_turns=%d",
            "YES" if result else "NO",
            message[:50],
            len(history) if history else 0,
        )
        return result

    except Exception as e:
        logger.warning("App context check failed: %s — assuming app present", str(e))
        return True  # fail open


async def classify(
    message: str,
    history: list[dict] | None = None,
) -> str:
    """
    Classifies user message into one of five intents.

    Args:
        message: The latest user message.
        history: Recent conversation turns { role, content }.

    Returns:
        One of: SEARCH | TICKET | RESOLVED | CASUAL | VAGUE
    """
    cleaned = message.strip().lower().rstrip("!?.")
    history = history or []

    # ── 1. Pure greeting fast-path — zero LLM cost ───────────────────────────
    # Only when no history — in-conversation "hi" goes to LLM for proper context
    if not history and cleaned in _GREETINGS:
        logger.info("Classifier: CASUAL (greeting fast-path) — '%s'", message[:40])
        return INTENT_CASUAL

    try:
        # ── 2. Intent classification ──────────────────────────────────────────
        history_str = ""
        if history:
            history_str = "Recent conversation:\n"
            for turn in history[-4:]:
                role    = "User" if turn.get("role") == "user" else "Assistant"
                content = str(turn.get("content", ""))[:200]
                history_str += f"{role}: {content}\n"
            history_str += "\n"

        response = await llm_client.fast.ainvoke([
            SystemMessage(content=_INTENT_SYSTEM),
            HumanMessage(content=_USER_TEMPLATE.format(
                history=history_str,
                message=message,
            )),
        ])

        raw    = response.content.strip().upper()
        intent = raw.split()[0] if raw else INTENT_SEARCH

        if intent not in VALID_INTENTS:
            logger.warning(
                "Classifier returned unknown intent '%s' — defaulting to SEARCH",
                intent,
            )
            return INTENT_SEARCH

        # ── 3. App context check for SEARCH intent ────────────────────────────
        # If SEARCH but no app established anywhere → VAGUE
        # Checks BOTH current message AND history turns together
        # Prevents: "hi" + greeting + "I have time sync issue" → wrong SEARCH
        # Allows:   "I have Astro issue" + "still broken" → SEARCH (Astro in history)
        if intent == INTENT_SEARCH:
            app_established = await _check_app_context(message, history)
            if not app_established:
                logger.info(
                    "Classifier: VAGUE (no app context in message or history) — '%s'",
                    message[:60],
                )
                return INTENT_VAGUE

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