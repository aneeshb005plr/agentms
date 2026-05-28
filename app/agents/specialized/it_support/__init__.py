# app/agents/specialized/it_support/tools/__init__.py
# Exports all agent tools as a single list.
# Import this in conversational_support_agent.py:
#   from app.agents.specialized.it_support.tools import AGENT_TOOLS
#
# Phase 2 note:
#   check_app_health — currently returns HEALTH_DATA_UNAVAILABLE (Phase 1 stub).
#   Agent must NEVER mention health check results to the user — they are internal
#   signals only. See system prompt strict rules.
#   Phase 2: will return real Dataverse health data when integration is complete.

from app.agents.specialized.it_support.tools.search_tool import search_knowledge_base
from app.agents.shared.tools.ticket_tool  import get_servicenow_link
from app.agents.shared.tools.health_tool  import check_app_health

# All tools — Phase 1
# check_app_health always returns HEALTH_DATA_UNAVAILABLE in Phase 1
# Agent uses it as a signal to continue with KB troubleshooting
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