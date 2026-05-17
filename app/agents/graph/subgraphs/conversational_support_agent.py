# app/agents/graph/subgraphs/conversational_support_agent.py
# Conversational Support Agent — Phase 1.
#
# Uses LangChain 1.0 create_agent (NOT deprecated create_react_agent).
# Built on LangGraph runtime with middleware architecture.
#
# Agent behaviour (enforced via system prompt + tool docstrings):
#   Greetings        → responds warmly, no tools
#   Out of scope     → politely declines, no tools
#   IT question      → calls search_knowledge_base (1 or N times)
#   Vector answer    → enriches with conversational context
#   Ticket needed    → calls get_servicenow_link (never automatic)
#   App health       → calls check_app_health if app_identified (Phase 2)
#   Follow-up        → uses trimmed history + conversation_summary (Layers 1+2)
#
# Middleware stack:
#   1. MessageTrimmerMiddleware (before_model) — trim history + inject summary
#   2. TokenTrackerMiddleware   (after_model)  — extract + accumulate token usage
#
# Token tracking:
#   - TokenTrackerMiddleware appends one entry per LLM call to state
#   - operator.add reducer accumulates all calls in current_message_llm_calls
#   - SSE endpoint reads this after stream completes → saves to token_usage collection
#
# Memory:
#   - Layer 1: MessageTrimmerMiddleware trims before every LLM call
#   - Layer 2: conversation_summary injected as SystemMessage when available
#   - Layer 3: retrieved_memory — always None in Phase 1
#
# app_identified:
#   - Derived from search results after vector tool call
#   - Extracted via post_agent processing in SSE endpoint
#   - Informational only — Phase 2 Dataverse hook

import logging

from langchain.agents import create_agent
from langchain_core.messages import SystemMessage

from app.agents.clients.llm_client import llm_client
from app.agents.middleware.message_trimmer import MessageTrimmerMiddleware
from app.agents.middleware.token_tracker import TokenTrackerMiddleware
from app.agents.tools import AGENT_TOOLS
from app.domains.prompts.service import PromptService
from app.domains.prompts.cache import PromptCache

logger = logging.getLogger(__name__)


async def build_conversational_support_agent(
    prompt_service: PromptService,
):
    """
    Builds and returns the compiled conversational_support_agent.
    Called once at startup in master_graph.py.

    Steps:
    1. Load system prompt from MongoDB via PromptService (falls back to defaults.py)
    2. Build middleware stack
    3. Create agent via create_agent with smart LLM + tools + middleware
    4. Return compiled agent graph

    Args:
        prompt_service: PromptService instance for loading system prompt from MongoDB

    Returns:
        Compiled LangGraph agent — ready for astream_events()
    """
    # Load system prompt — MongoDB → cache → defaults.py fallback
    system_prompt = await prompt_service.get_prompt(
        agent_id=PromptCache.CONVERSATIONAL_SUPPORT_AGENT,
        prompt_key=PromptCache.SYSTEM_PROMPT,
    )

    logger.info(
        "Building conversational_support_agent — "
        "model=%s tools=%d",
        llm_client.smart.model_name if hasattr(llm_client.smart, 'model_name') else 'configured',
        len(AGENT_TOOLS),
    )

    # Middleware stack
    # Order matters:
    #   before_model hooks run in list order (trimmer first)
    #   after_model hooks run in REVERSE list order (tracker last added = first to run)
    middleware = [
        MessageTrimmerMiddleware(),
        TokenTrackerMiddleware(),
    ]

    # Build agent using LangChain 1.0 create_agent
    agent = create_agent(
        model=llm_client.smart,      # smart LLM — gpt-4o for reasoning
        tools=AGENT_TOOLS,           # search, ticket, health check
        prompt=system_prompt,        # loaded from MongoDB
        middleware=middleware,        # trimmer + token tracker
    )

    logger.info("conversational_support_agent built successfully")
    return agent


def extract_app_identified(search_results: list[dict] | None) -> str | None:
    """
    Derives app_identified from search results stored in state.
    Called by SSE endpoint after agent stream completes.

    Finds most frequent non-general application in top chunks.
    Returns None if mixed results or no clear winner.

    This is informational only — used for Phase 2 health check hook.
    """
    if not search_results:
        return None

    from collections import Counter
    from app.config import settings

    apps = [
        r.get("application")
        for r in search_results
        if r.get("application") and not r.get("is_general", False)
    ]

    if not apps:
        return None

    app_counts = Counter(apps)
    most_common, count = app_counts.most_common(1)[0]

    if count >= settings.VECTOR_APP_IDENTIFICATION_MIN_CHUNKS:
        return most_common

    return None