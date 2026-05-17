# app/agents/tools/ticket_tool.py
# ServiceNow ticket tool — returns ticket URL for user to raise a request.
#
# Phase 1: returns configured SERVICENOW_TICKET_URL — manual link only.
# Phase 2: will create actual ServiceNow ticket via API and return ticket number.
#
# IMPORTANT — agent calling rules (enforced via docstring):
# - Do NOT call on first response — always try knowledge base first
# - Call ONLY when:
#     1. User has tried troubleshooting steps and issue still not resolved
#     2. User explicitly asks to raise a ticket
#     3. No information found AND issue seems genuine/urgent
# - Never call just because no search results were found — suggest gently instead

import logging

from langchain_core.tools import tool

from app.config import settings

logger = logging.getLogger(__name__)


@tool
async def get_servicenow_link() -> str:
    """
    Get the ServiceNow link for the user to raise an IT support ticket.

    Use this tool ONLY in these situations:
    1. The user has already tried the troubleshooting steps and the issue is still not resolved
    2. The user explicitly asks to raise a ticket or get support
    3. No information was found in the knowledge base AND the user's issue is genuine

    Do NOT use this tool:
    - On the first response before trying the knowledge base
    - Just because no search results were found (suggest it gently in text instead)
    - For greetings or out-of-scope questions
    - Multiple times in the same conversation turn

    When you use this tool, include the link naturally in your response with a
    brief explanation of why they should raise a ticket and what information
    to include in the ticket description.

    Returns:
        ServiceNow ticket URL for the user to raise an IT support request.
    """
    logger.info("get_servicenow_link called")

    if not settings.SERVICENOW_TICKET_URL:
        logger.warning("SERVICENOW_TICKET_URL not configured")
        return (
            "TICKET_URL_NOT_CONFIGURED: ServiceNow URL not configured. "
            "Please inform the user to contact IT support directly."
        )

    return (
        f"SERVICENOW_LINK: {settings.SERVICENOW_TICKET_URL}\n"
        "Include this link in your response with a clear call-to-action. "
        "Suggest the user describe their issue, steps already tried, "
        "and any error messages they saw."
    )