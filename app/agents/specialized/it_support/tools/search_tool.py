# app/agents/specialized/it_support/tools/search_tool.py
# Knowledge base search tool — wraps VectorClient.
#
# Passes full chunk text to agent — not truncated.
# Agent uses ANSWER as structure + chunk text for detail.
# This gives the agent everything it needs for rich, complete responses.

import json
import logging

from langchain_core.tools import tool

from app.agents.shared.clients.vector_client import VectorChunk, vector_client

logger = logging.getLogger(__name__)


def _format_chunk(chunk: VectorChunk, index: int) -> str:
    """
    Formats a cited chunk with FULL text for agent context.
    Full text gives the agent maximum detail to work with.
    """
    lines = [f"[Source {index + 1}]"]
    if chunk.file_name:
        lines.append(f"File: {chunk.file_name}")
    if chunk.application:
        lines.append(f"Application: {chunk.application}")
    if chunk.source_url and chunk.source_url.startswith("http"):
        lines.append(f"URL: {chunk.source_url}")
    # Pass FULL chunk text — agent uses it to enrich and complete the response
    # Truncating to 200 chars was losing critical detail (contacts, steps, etc.)
    if chunk.text:
        lines.append(f"Content:\n{chunk.text}")
    return "\n".join(lines)


@tool
async def search_knowledge_base(query: str) -> str:
    """
    Search the PwC IT support knowledge base for information related to the query.

    Use this tool when the user asks an IT support question, how-to question,
    or guidance request about any PwC application or system.

    Call this tool with a clear, specific search query. For follow-up questions,
    include context from the conversation history in the query.

    Args:
        query: Clear, specific search query including app name and context.
               Examples:
                 "SAP login authentication failure"
                 "Workday timesheet submission steps"
                 "USRPP application point of contact"
                 "how to request VPN access PwC"

    Returns:
        Pre-synthesised answer with full cited source content,
        or NO_RESULTS_FOUND if knowledge base has no relevant information.
    """
    logger.info("search_knowledge_base called — query: %s", query)

    try:
        result = await vector_client.search(query)
    except Exception as e:
        logger.error("Vector search failed: %s", str(e))
        return (
            "SEARCH_ERROR: Knowledge base search failed due to a technical issue. "
            "Inform the user and suggest raising a ServiceNow ticket if urgent."
        )

    # answer_available is the authoritative signal
    if not result.answer_available:
        return (
            f"NO_RESULTS_FOUND: No relevant information found for query: '{query}'. "
            "Do not guess or make up information. "
            "Inform the user this topic is not currently in the knowledge base. "
            "For IT problems suggest raising a ticket. "
            "For guidance questions suggest checking internal documentation."
        )

    output_parts = [f"SEARCH_RESULTS for query: '{query}'", ""]

    if result.app_identified:
        output_parts.append(f"Application identified: {result.app_identified}")
        output_parts.append("")

    # Embed cited sources for citation extraction by chat.py
    sources_data = [
        {
            "file_name":   chunk.file_name,
            "source_url":  chunk.source_url,
            "application": chunk.application or "",
        }
        for chunk in result.chunks
        if chunk.file_name
    ]
    if sources_data:
        output_parts.append(f"SOURCES_JSON:{json.dumps(sources_data)}")
        output_parts.append("")

    # Primary synthesised answer — use as the structure of your response
    output_parts.extend([
        "SYNTHESISED ANSWER (use as structure):",
        result.answer or "",
        "",
    ])

    # Full cited chunk content — use to enrich and complete the response
    # These chunks were CITED by the LLM when building the answer above.
    # They contain the full original text — use it to add detail, contacts,
    # steps, or any information the synthesised answer may have summarised.
    if result.chunks:
        output_parts.append(
            f"FULL SOURCE CONTENT ({result.cited_chunks} cited articles):"
        )
        output_parts.append(
            "Use this content to enrich your response with complete details. "
            "Do not summarise away important specifics like names, emails, "
            "step numbers, error codes, or contact details."
        )
        output_parts.append("")
        for i, chunk in enumerate(result.chunks):
            output_parts.append(_format_chunk(chunk, i))
            output_parts.append("")

    return "\n".join(output_parts)