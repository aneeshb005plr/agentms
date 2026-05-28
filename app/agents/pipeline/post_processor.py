# app/agents/pipeline/post_processor.py
# Post-processing of agent response before streaming to user.
#
# Responsibilities (single pass over final_content):
#   1. Strip duplicate ServiceNow markdown links when ticket_url is set
#      Formatter may add [text](servicenow-url) even though pipeline
#      already renders a ticket button via ticket_url in the frontend.
#
#   2. Strip orphaned ticket sentence when ticket_url is null
#      "I have provided a support ticket link below" must only appear
#      when a ticket button is actually rendered. If ticket_url is null,
#      this sentence was hallucinated by agent or formatter — remove it.
#
# Design:
#   Plain functions — no class needed.
#   Single pass regex — no multiple string traversals.
#   Fails open — any error returns original content unchanged.
#
# Called by: orchestrator.py after formatter.format_response()

import logging
import re

logger = logging.getLogger(__name__)

# Sentence that must only appear when ticket_url is set
_TICKET_SENTENCE_PATTERN = re.compile(
    r"I have provided a support ticket link below\.?",
    re.IGNORECASE,
)

# ServiceNow markdown link pattern — [any text](servicenow-url)
_SERVICENOW_LINK_PATTERN = re.compile(
    r"\[([^\]]+)\]\(https?://[^)]*(?:service-now|servicenow)[^)]*\)",
    re.IGNORECASE,
)


def process(content: str, ticket_url: str | None, session_id: str) -> str:
    """
    Post-processes formatted response content.
    Single pass — applies all rules in sequence.

    Args:
        content:    Formatted markdown response from formatter.
        ticket_url: ServiceNow URL if ticket was provided, else None.
        session_id: For logging only.

    Returns:
        Cleaned content ready to stream to user.
    """
    if not content:
        return content

    try:
        if ticket_url:
            content = _strip_duplicate_ticket_links(content, session_id)
        else:
            content = _strip_orphaned_ticket_sentence(content, session_id)
    except Exception as e:
        logger.warning(
            "Post-processor failed for session %s: %s — returning original",
            session_id, str(e),
        )

    return content


def _strip_duplicate_ticket_links(content: str, session_id: str) -> str:
    """
    Strips [text](servicenow-url) markdown links when ticket_url is set.
    The frontend renders a proper ticket button from ticket_url —
    a markdown link alongside it creates a duplicate confusing experience.
    Keeps the link text, removes the URL part.
    """
    cleaned, count = _SERVICENOW_LINK_PATTERN.subn(r"\1", content)
    if count:
        logger.info(
            "Post-processor: stripped %d duplicate ServiceNow link(s) "
            "for session %s", count, session_id,
        )
    return cleaned


def _strip_orphaned_ticket_sentence(content: str, session_id: str) -> str:
    """
    Strips 'I have provided a support ticket link below' when ticket_url is null.
    This sentence is only meaningful when a ticket button is rendered.
    Without ticket_url the button never appears — sentence is misleading.
    """
    if not _TICKET_SENTENCE_PATTERN.search(content):
        return content

    cleaned = _TICKET_SENTENCE_PATTERN.sub("", content).strip()
    logger.info(
        "Post-processor: stripped orphaned ticket sentence "
        "(ticket_url is null) for session %s", session_id,
    )
    return cleaned