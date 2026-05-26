# app/agents/pipeline/responses.py
# Canned responses for pre-classifier outcomes.
#
# These responses are returned directly by chat.py without calling the agent.
# Kept here so they can be updated without touching routing logic.
# In Phase 2: move these to MongoDB prompts collection for admin UI editing.

GREETING = (
    "Hello! I'm NextGenAMS, your PwC IT support assistant. "
    "How can I help you today? Please describe the application "
    "you're having trouble with and what specifically is happening."
)

CLARIFICATION = (
    "I'd be happy to help. To point you in the right direction, "
    "could you please tell me which PwC application or system you're asking about, "
    "and what you're trying to do or what issue you're experiencing? "
    "For example: which app, what you need help with, or what error you're seeing."
)


def get(classification: str) -> str:
    """
    Returns the canned response text for a given classification.
    Raises ValueError for unknown classifications — only 'greeting' and 'vague' expected.
    """
    if classification == "greeting":
        return GREETING
    if classification == "vague":
        return CLARIFICATION
    raise ValueError(
        f"No canned response for classification '{classification}'. "
        "Only 'greeting' and 'vague' have canned responses."
    )