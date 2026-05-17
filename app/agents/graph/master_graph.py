# app/agents/graph/master_graph.py
# Master graph — builds and holds the conversational_support_agent.
#
# Built once at startup → stored as singleton → reused across all requests.
#
# Checkpointer:
#   Uses MongoDBSaver (sync) — AsyncMongoDBSaver removed in v0.3.0.
#   FastAPI runs sync checkpointer calls in thread pool automatically.
#   MongoDBSaver needs a sync MongoClient — dedicated one created here.
#   Our existing AsyncMongoClient (db.mongo_client) is for our own collections.
#   MongoDBSaver creates indexes automatically on __init__ — no setup() call needed.
#
# create_agent returns an ALREADY COMPILED graph:
#   Do NOT call .compile() on it — will fail.
#   Pass checkpointer directly into create_agent() via checkpointer= parameter.
#
# thread_id = conversation_id:
#   LangGraph uses thread_id to load/save checkpoints.
#   Same conversation_id across sessions = full persistent memory.

import logging

from pymongo import MongoClient
from langgraph.checkpoint.mongodb import MongoDBSaver

from app.config import settings
from app.agents.graph.subgraphs.conversational_support_agent import (
    build_conversational_support_agent,
)
from app.domains.prompts.service import PromptService

logger = logging.getLogger(__name__)


class MasterGraph:
    """
    Holds the compiled LangGraph agent.
    Singleton — initialised once at startup via build().
    """

    def __init__(self):
        self._graph       = None
        self._sync_client: MongoClient | None = None
        self._built:       bool = False

    async def build(self, prompt_service: PromptService) -> None:
        """
        Builds the agent graph with checkpointer.
        Called once in FastAPI lifespan startup.

        Steps:
        1. Create dedicated sync MongoClient for checkpointer
        2. Create MongoDBSaver — indexes created automatically
        3. Build conversational_support_agent — passes checkpointer into create_agent()
           create_agent() returns already-compiled graph — no .compile() needed
        """
        logger.info("Building master graph...")

        # Step 1 — Dedicated sync MongoClient for checkpointer
        # Separate from our AsyncMongoClient — MongoDBSaver needs sync client
        self._sync_client = MongoClient(
            settings.MONGODB_URI,
            maxPoolSize=5,    # small pool — checkpointer only
        )

        # Step 2 — MongoDBSaver
        # Indexes created automatically in __init__ — no setup() call needed
        checkpointer = MongoDBSaver(
            client=self._sync_client,
            db_name=settings.MONGODB_DB_NAME,
        )
        logger.info("MongoDBSaver checkpointer ready — indexes auto-created")

        # Step 3 — Build agent
        # create_agent() returns an ALREADY COMPILED graph
        # checkpointer passed directly into create_agent() — NOT via .compile()
        self._graph = await build_conversational_support_agent(
            prompt_service=prompt_service,
            checkpointer=checkpointer,
        )

        self._built = True
        logger.info("Master graph built successfully")

    def close(self) -> None:
        """Closes sync MongoClient at FastAPI shutdown."""
        if self._sync_client:
            self._sync_client.close()
            logger.info("Master graph sync MongoClient closed")

    @property
    def graph(self):
        """
        Returns compiled graph for use in SSE endpoint.

        Usage in chat.py:
            config = {"configurable": {"thread_id": conversation_id}}
            async for event in master_graph.graph.astream_events(
                input={
                    "messages":                  [HumanMessage(content=user_message)],
                    "session_id":                conversation_id,
                    "user_id":                   user_id,
                    "current_message_llm_calls": [],
                    "requires_ticket":           False,
                    "search_results":            None,
                    "user_intent":               None,
                    "search_queries":            None,
                    "app_identified":            None,
                    "health_data":               None,
                    "conversation_summary":      None,
                    "retrieved_memory":          None,
                },
                config=config,
                version="v2",
            ):
                # handle event
        """
        if not self._built or self._graph is None:
            raise RuntimeError(
                "Master graph not built. Call build() first in lifespan startup."
            )
        return self._graph


# Singleton
master_graph = MasterGraph()