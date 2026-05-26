# app/agents/pipeline/__init__.py
# Pipeline package — pre-agent message processing.
#
# Modules:
#   classifier  — classify message: "greeting" | "vague" | "search"
#   responses   — canned responses for greeting and vague classifications
#   suggestions — grounded follow-up question generation
#
# Import pattern in chat.py:
#   from app.agents.pipeline import classifier, responses, suggestions

from app.agents.pipeline import classifier
from app.agents.pipeline import responses
from app.agents.pipeline import suggestions

__all__ = ["classifier", "responses", "suggestions"]