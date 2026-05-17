# app/agents/tools/__init__.py
# Exports all agent tools as a single list.
# Import this in conversational_support_agent.py:
#   from app.agents.tools import AGENT_TOOLS

from app.agents.tools.search_tool import search_knowledge_base
from app.agents.tools.ticket_tool import get_servicenow_link
from app.agents.tools.health_tool import check_app_health

# All tools available to conversational_support_agent
AGENT_TOOLS = [
    search_knowledge_base,
    get_servicenow_link,
    check_app_health,
]

__all__ = [
    "AGENT_TOOLS",
    "search_knowledge_base",
    "get_servicenow_link",
    "check_app_health",
]