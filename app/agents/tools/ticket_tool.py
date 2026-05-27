# app/agents/tools/ticket_tool.py
# ServiceNow ticket tool — returns ticket URL.
#
# MCP-ready: when ServiceNow MCP server is available, replace this tool
# with mcp_create_ticket() which creates the ticket directly and returns
# a ticket number. The TICKET intent handler in chat.py will use it.
#
# Returns SERVICENOW_LINK:{url} — chat.py extracts URL via regex from on_tool_end.

import logging

from langchain_core.tools import tool

from app.config import settings

logger = logging.getLogger(__name__)


@tool
async def get_servicenow_link() -> str:
    """
    Get the ServiceNow URL for the user to raise an IT support ticket.

    Call this tool when:
    1. The user has tried troubleshooting steps and the issue is still not resolved
    2. The user explicitly asks to raise a ticket or escalate

    After calling this tool:
    - Say ONLY: "I have provided a support ticket link below."
    - Do NOT include the raw URL in your response text
    - The system renders a button automatically from the URL

    Never call this tool multiple times in one response.
    """
    url = getattr(settings, "SERVICENOW_URL", None) or "https://pwc.service-now.com/sp"

    logger.info("get_servicenow_link called — returning URL: %s", url)

    return f"SERVICENOW_LINK:{url}"