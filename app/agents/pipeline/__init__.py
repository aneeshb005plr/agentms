# app/agents/pipeline/__init__.py
# Pipeline package — pre-agent processing + post-agent formatting.
#
# Modules:
#   classifier  — classify message: SEARCH | TICKET | RESOLVED | CASUAL | VAGUE
#   responses   — LLM responses for non-search intents + escalation detection
#   suggestions — grounded follow-up question generation
#   formatter   — markdown formatting of agent plain text responses

from app.agents.pipeline import classifier
from app.agents.pipeline import responses
from app.agents.pipeline import suggestions
from app.agents.pipeline import formatter

__all__ = ["classifier", "responses", "suggestions", "formatter"]