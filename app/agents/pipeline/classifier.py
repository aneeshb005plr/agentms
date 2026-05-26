# app/agents/pipeline/classifier.py
# Message pre-classifier — runs before the agent on every message.
#
# Responsibility:
#   Decide whether a message should:
#     "greeting" → return a warm response, skip agent + vector
#     "vague"    → return clarification question, skip agent + vector
#     "search"   → pass to agent unchanged
#
# Design:
#   Greeting detection — pure Python, zero LLM cost.
#     Fixed set of greeting strings. Only triggers on first conversation message.
#     Greetings are universal and don't change — small fixed set is fine.
#
#   Vague detection — one gpt-4o-mini call (~150ms).
#     Single YES/NO question: "is this a specific IT problem?"
#     Uses mini model — cheap, fast, handles all edge cases including
#     future apps, typos, unusual phrasing. No maintenance needed.
#     Fails open → defaults to "search" (never blocks a real IT question).
#
#   Everything else → "search" → agent runs normally.
#
# Called by: app/api/v1/chat.py — before agent invocation.

import logging

logger = logging.getLogger(__name__)

# Greeting strings — checked against first message only
# Lowercase, stripped. Not a maintenance problem — greetings are universal.
_GREETINGS: frozenset[str] = frozenset({
    "hi", "hello", "hey", "hiya", "howdy",
    "good morning", "good afternoon", "good evening", "good day",
    "hi there", "hello there", "hey there",
    "greetings", "sup", "what's up", "whats up",
})

# Classifier prompt — single YES/NO question to gpt-4o-mini
_CLASSIFY_SYSTEM = (
    "You classify IT support messages. "
    "Answer ONLY with YES or NO. No explanation."
)

_CLASSIFY_USER = (
    "Does this message contain a specific IT question, problem, or request "
    "for guidance about a PwC application or system?\n\n"
    "Answer YES if the message:\n"
    "  - Describes an IT problem or error (e.g. cannot login, getting error 403)\n"
    "  - Asks how to do something in an app (e.g. how do I submit timesheet in Workday)\n"
    "  - Requests guidance or steps (e.g. steps to request software installation)\n"
    "  - Asks for information about a PwC app or system (e.g. what is Astro used for)\n"
    "  - Mentions a specific application name with any context\n\n"
    "Answer NO if the message:\n"
    "  - Is only a greeting with no IT content (e.g. hi, hello, good morning)\n"
    "  - Is completely vague with no application and no context (e.g. I need help, "
    "something is wrong, I have an issue)\n"
    "  - Is clearly unrelated to IT or PwC systems (e.g. weather, personal questions)\n\n"
    "Message: {message}\n\n"
    "Answer YES or NO:"
)


async def classify(message: str, is_first_message: bool) -> str:
    """
    Classifies a message before passing to the agent.

    Args:
        message:          Raw user message text.
        is_first_message: True if this is the first message in the conversation.

    Returns:
        "greeting" — warm response, skip agent
        "vague"    — clarification question, skip agent
        "search"   — pass to agent as normal
    """
    cleaned = message.strip().lower().rstrip("!?.")

    # ── Greeting — pure Python, zero cost ────────────────────────────────────
    if is_first_message and cleaned in _GREETINGS:
        logger.info("Classifier: greeting detected")
        return "greeting"

    # ── Vague — gpt-4o-mini single YES/NO call ────────────────────────────────
    try:
        from app.agents.clients.llm_client import llm_client
        from langchain_core.messages import HumanMessage, SystemMessage

        response = await llm_client.fast.ainvoke([
            SystemMessage(content=_CLASSIFY_SYSTEM),
            HumanMessage(content=_CLASSIFY_USER.format(message=message)),
        ])

        answer = response.content.strip().upper()

        if answer.startswith("NO"):
            logger.info("Classifier: vague — '%s'", message[:60])
            return "vague"

        logger.info("Classifier: search — '%s'", message[:60])
        return "search"

    except Exception as e:
        # Fail open — never block a real IT question due to classifier error
        logger.warning(
            "Classifier failed: %s — defaulting to search", str(e)
        )
        return "search"