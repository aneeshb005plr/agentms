# app/agents/pipeline/formatter.py
# Response formatter — takes agent's plain text and applies proper markdown structure.
#
# Separation of concerns:
#   Agent  → decides WHAT to say (accuracy, knowledge)
#   Formatter → decides HOW to display it (structure, hierarchy, readability)
#
# Why a separate formatter instead of system prompt instructions:
#   - Agent's training data has billions of plain prose responses
#   - Formatting instructions in system prompt compete with training priors
#   - Formatter has ONE job with no ambiguity — always produces markdown
#   - gpt-4o-mini is fast (~150-200ms) and very reliable for reformatting tasks
#
# Response types handled:
#   troubleshooting — numbered steps, bold actions, error codes
#   contact         — bold role headers, names, emails grouped by role
#   informational   — bold section headers, bullet points per section
#   general         — sensible defaults for anything else
#
# Called by: app/api/v1/chat.py — after agent completes, before streaming to user.
# Fails open: formatter error → return original plain text unchanged.

import logging

logger = logging.getLogger(__name__)

_FORMATTER_SYSTEM = (
    "You are a markdown formatter for a PwC IT support chatbot. "
    "Your ONLY job is to reformat the given response text using proper markdown. "
    "Do NOT change the meaning, add new information, or remove any details. "
    "Preserve ALL content — names, emails, steps, error codes, URLs.\n\n"

    "Apply these formatting rules based on content type:\n\n"

    "For TROUBLESHOOTING responses (contain steps to fix something):\n"
    "  - Add **Steps to resolve:** or **Here's how to [action]:** as a bold header\n"
    "  - Number each step: 1. **Verb** — description\n"
    "  - Bold the action verb in each step\n"
    "  - Indent sub-points with - under the relevant step\n"
    "  - Bold any error codes mentioned (e.g. **ZEME:019**)\n\n"

    "For CONTACT / POC responses (contain names, roles, emails):\n"
    "  - Group contacts under ## bold role headers\n"
    "  - Each person on their own line: **Name** — Role\n"
    "  - Email on next line indented: Email: name@pwc.com\n"
    "  - Bold all names and email addresses\n\n"

    "For INFORMATIONAL responses (explain what something is or how it works):\n"
    "  - Use ## headers for each distinct topic or section\n"
    "  - Use **bold** for key terms, system names, team names\n"
    "  - Use bullet points - for lists of items\n"
    "  - Keep paragraphs short — max 2 sentences before a break\n\n"

    "General rules for ALL responses:\n"
    "  - **Bold** all: application names, team names, person names, error codes\n"
    "  - Add blank line between sections for breathing room\n"
    "  - Never output plain unstructured paragraphs\n"
    "  - Return ONLY the reformatted markdown — no preamble, no explanation\n\n"
    "STRICT URL AND LINK RULES (most important):\n"
    "  - NEVER add new hyperlinks or markdown links not present in the original text\n"
    "  - NEVER convert plain text sentences into hyperlinks\n"
    "  - If original text has a URL (e.g. https://astro.pwc.com) preserve it exactly as-is\n"
    "  - The sentence 'I have provided a support ticket link below' must be preserved\n"
    "    EXACTLY as written — never bold it, link it, or modify it in any way\n"
    "  - The ticket button is rendered separately by the system — never add ticket links\n"
)


async def format_response(plain_text: str) -> str:
    """
    Reformats agent plain text response into proper markdown.

    Args:
        plain_text: Raw agent response text.

    Returns:
        Markdown-formatted version of the same content.
        Falls back to original text if formatting fails.
    """
    if not plain_text or not plain_text.strip():
        return plain_text

    # Skip formatting for very short responses — greetings, one-liners
    # They don't need structure and formatting would look odd
    if len(plain_text.strip()) < 100:
        return plain_text

    try:
        from app.agents.clients.llm_client import llm_client
        from langchain_core.messages import HumanMessage, SystemMessage

        response = await llm_client.fast.ainvoke([
            SystemMessage(content=_FORMATTER_SYSTEM),
            HumanMessage(content=f"Reformat this response:\n\n{plain_text}"),
        ])

        formatted = response.content.strip()

        if not formatted:
            logger.warning("Formatter returned empty response — using original")
            return plain_text

        logger.debug("Formatter: %d chars → %d chars", len(plain_text), len(formatted))
        return formatted

    except Exception as e:
        logger.warning("Formatter failed: %s — using original plain text", str(e))
        return plain_text  # fail open — always return something