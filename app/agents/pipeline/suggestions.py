# app/agents/pipeline/suggestions.py
# Grounded follow-up question suggestion generator.
#
# Grounding strategy — two layers:
#   Layer 1: Answer text (primary) — anything mentioned in the answer is
#            definitely in the knowledge base. This is the primary ground truth.
#   Layer 2: Cited chunk metadata (secondary) — related topics in same documents
#            that the answer may not have fully covered but are still answerable.
#
# Both layers together give the LLM rich but validated context.
# Suggestions generated from answer text are guaranteed answerable.
# Suggestions from chunk metadata are highly likely answerable.
#
# Returns empty list when:
#   - No answer text (stream was stopped before agent responded)
#   - No search results (answer_available=False — no KB content)
#   - LLM call fails (silent — never breaks the response flow)
#
# Called by: app/api/v1/chat.py — after stream completes.

import json
import logging
import re

from langchain_core.messages import HumanMessage, SystemMessage
from app.agents.shared.clients.llm_client import llm_client
logger = logging.getLogger(__name__)


async def generate(
    search_results: list[dict],
    answer_text:    str,
) -> list[str]:
    """
    Generates up to 2 grounded follow-up question suggestions.

    Args:
        search_results: List of cited source dicts { file_name, source_url, application }.
                        Empty when answer_available=False.
        answer_text:    The full assistant response text just generated.

    Returns:
        List of 0-2 suggestion strings.
    """
    if not answer_text or not search_results:
        return []

    try:
        from app.domains.prompts.cache import prompt_cache, PromptCache

        # Load prompt from cache (MongoDB → defaults.py fallback)
        prompt_template = prompt_cache.get(
            PromptCache.SUGGESTION_GENERATION,
            PromptCache.SUGGESTION_PROMPT,
        )
        if not prompt_template:
            from app.domains.prompts.defaults import SUGGESTION_QUESTIONS_PROMPT
            prompt_template = SUGGESTION_QUESTIONS_PROMPT

        # Layer 1 — answer text (primary grounding, first 800 chars)
        answer_excerpt = answer_text.strip()[:800]

        # Layer 2 — cited chunk metadata (secondary grounding)
        chunk_lines: list[str] = []
        for s in search_results[:4]:
            file_name = s.get("file_name", "")
            app       = s.get("application", "")
            excerpt   = s.get("excerpt", "")
            if file_name:
                line = f"- [{file_name}]"
                if app:
                    line += f" ({app})"
                if excerpt:
                    line += f": {excerpt[:150]}"
                chunk_lines.append(line)

        chunk_context = "\n".join(chunk_lines) if chunk_lines else "See cited articles above."

        prompt = prompt_template.format(
            answer_text=answer_excerpt,
            chunk_context=chunk_context,
        )

        response = await llm_client.fast.ainvoke([HumanMessage(content=prompt)])
        raw      = response.content.strip()

        match = re.search(r'\[.*?\]', raw, re.DOTALL)
        if match:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, list):
                return [
                    str(s)[:100]
                    for s in parsed[:2]
                    if isinstance(s, str) and len(s.strip()) > 5
                ]

    except Exception as e:
        logger.debug("Suggestion generation failed: %s", str(e))

    return []