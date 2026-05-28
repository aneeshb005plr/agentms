# app/agents/specialized/it_support/graph.py
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

import logging
from collections import Counter

from langchain.agents import create_agent

from app.agents.shared.clients.llm_client import llm_client
from app.agents.middleware.message_trimmer import MessageTrimmerMiddleware
from app.agents.middleware.token_tracker import TokenTrackerMiddleware
from app.agents.specialized.it_support.tools import AGENT_TOOLS
from app.config import settings
from app.domains.prompts.cache import PromptCache
from app.domains.prompts.service import PromptService

logger = logging.getLogger(__name__)

# Agent name constant — used in token tracking
AGENT_NAME = "conversational_support_agent"


async def build_conversational_support_agent(
    prompt_service: PromptService,
    checkpointer=None,
):
    """
    Builds and returns the compiled conversational_support_agent.
    Called once at startup in master_graph.py.

    Steps:
    1. Load system prompt from MongoDB via PromptService (falls back to defaults.py)
    2. Build middleware stack
    3. Create agent via create_agent with smart LLM + tools + middleware + checkpointer
       create_agent returns an ALREADY COMPILED graph — no .compile() needed

    Args:
        prompt_service: PromptService instance for loading system prompt from MongoDB
        checkpointer:   MongoDBSaver instance for persistent conversation memory

    Returns:
        Compiled LangGraph agent — ready for astream_events()
    """
    # Load system prompt — MongoDB → cache → defaults.py fallback
    system_prompt = await prompt_service.get_prompt(
        agent_id=PromptCache.CONVERSATIONAL_SUPPORT_AGENT,
        prompt_key=PromptCache.SYSTEM_PROMPT,
    )

    logger.info(
        "Building conversational_support_agent — model=%s tools=%d",
        getattr(llm_client.smart, 'model_name', 'configured'),
        len(AGENT_TOOLS),
    )

    # Middleware stack
    # before_model hooks run in list order
    # after_model hooks run in REVERSE list order
    middleware = [
        MessageTrimmerMiddleware(),
        TokenTrackerMiddleware(),
    ]

    # Build agent — create_agent returns ALREADY COMPILED graph
    agent = create_agent(
        model=llm_client.smart,
        tools=AGENT_TOOLS,
        system_prompt=system_prompt,
        middleware=middleware,
        checkpointer=checkpointer,
    )

    logger.info("conversational_support_agent built successfully — compiled graph returned")
    return agent


def extract_app_identified(search_results: list[dict] | None) -> str | None:
    """
    Derives app_identified from vector search results stored in state.
    Called by SSE endpoint after agent stream completes.

    Finds most frequent non-general application in top chunks.
    Returns None if mixed results or no clear winner.

    Informational only — used for Phase 2 health check hook.
    """
    if not search_results:
        return None

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