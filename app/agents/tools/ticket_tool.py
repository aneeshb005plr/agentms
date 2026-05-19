# app/agents/tools/ticket_tool.py
# ServiceNow ticket tool — returns ticket URL.
#
# Returns ONLY the URL after SERVICENOW_LINK: prefix.
# The instruction text is kept minimal so the LLM includes the link naturally.
# Backend chat.py extracts the URL using regex — robust against any extra text.

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
    - Multiple times in the same conversation turn

    When you use this tool, include the link naturally in your response.
    Tell the user what information to include when raising the ticket.

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

    # Return clean URL with minimal instruction text
    # Backend extracts URL using regex so format is flexible
    return f"SERVICENOW_LINK: {settings.SERVICENOW_TICKET_URL}"