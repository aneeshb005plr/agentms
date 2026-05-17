# app/agents/graph/master_graph.py
# Master graph — compiles and holds the conversational_support_agent.
#
# Compiled once at startup → stored in app.state → reused across all requests.
# Compiling is expensive — never compile per request.
#
# Checkpointer:
#   Uses MongoDBSaver (sync) — AsyncMongoDBSaver was removed in v0.3.0.
#   FastAPI runs sync checkpointer calls in thread pool automatically.
#   MongoDBSaver needs a sync MongoClient — we create a dedicated one here.
#   Our existing AsyncMongoClient (db.mongo_client) is for our own collections.
#
# thread_id = conversation_id:
#   LangGraph uses thread_id to load/save checkpoints.
#   We set thread_id = conversation_id so every conversation has its own memory.
#   Same conversation_id across sessions = full persistent memory.
#
# Why not AsyncMongoDBSaver?
#   Removed in langgraph-checkpoint-mongodb v0.3.0.
#   MongoDBSaver (sync) is the only option now.
#   FastAPI handles sync-in-async correctly via run_in_executor.

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
    Holds the compiled LangGraph agent and MongoDB checkpointer.
    Singleton — initialised once at startup via build().
    Stored in FastAPI app.state for access across all requests.
    """

    def __init__(self):
        self._graph = None
        self._checkpointer: MongoDBSaver | None = None
        self._sync_client: MongoClient | None = None
        self._built: bool = False

    async def build(self, prompt_service: PromptService) -> None:
        """
        Builds and compiles the agent graph.
        Called once in FastAPI lifespan startup.

        Steps:
        1. Create dedicated sync MongoClient for checkpointer
        2. Initialise MongoDBSaver checkpointer
        3. Build conversational_support_agent (loads system prompt)
        4. Store compiled graph
        """
        logger.info("Building master graph...")

        # Step 1 — Dedicated sync MongoClient for checkpointer
        # Separate from our AsyncMongoClient — checkpointer needs sync client
        self._sync_client = MongoClient(
            settings.MONGODB_URI,
            maxPoolSize=5,    # small pool — checkpointer only
        )

        # Step 2 — MongoDBSaver checkpointer
        # db_name = our nextgenams DB — checkpoints stored alongside our collections
        self._checkpointer = MongoDBSaver(
            client=self._sync_client,
            db_name=settings.MONGODB_DB_NAME,
        )

        # Setup checkpointer indexes
        self._checkpointer.setup()
        logger.info("MongoDBSaver checkpointer initialised")

        # Step 3 — Build conversational_support_agent
        # Loads system prompt from MongoDB via PromptService
        agent = await build_conversational_support_agent(
            prompt_service=prompt_service,
        )

        # Step 4 — Compile with checkpointer
        # create_agent returns a compiled graph — we add checkpointer here
        self._graph = agent.compile(checkpointer=self._checkpointer)

        self._built = True
        logger.info("Master graph built and compiled successfully")

    def close(self) -> None:
        """
        Closes sync MongoClient.
        Called at FastAPI shutdown.
        """
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
                input={"messages": [HumanMessage(content=user_message)],
                       "session_id": conversation_id,
                       "user_id": user_id,
                       "current_message_llm_calls": [],
                       "requires_ticket": False},
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


# Singleton — stored in app.state at startup
master_graph = MasterGraph()