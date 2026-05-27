# app/agents/pipeline/__init__.py
# Pipeline package — pre-agent message processing.
#
# Modules:
#   classifier  — classify message: SEARCH | TICKET | RESOLVED | CASUAL | VAGUE
#   responses   — LLM responses for non-search intents + escalation detection
#   suggestions — grounded follow-up question generation

from app.agents.pipeline import classifier
from app.agents.pipeline import responses
from app.agents.pipeline import suggestions

__all__ = ["classifier", "responses", "suggestions"]