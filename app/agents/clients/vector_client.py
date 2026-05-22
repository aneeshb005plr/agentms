# app/agents/clients/vector_client.py
# HTTP client for PwC Vector API (knowledge base search).
#
# Endpoint:
#   POST https://webapp-docassist.east.dev.ngc.pwcinternal.com
#        /api/vector-retrieval/api/v1/nextgenams_dev/query
#
# New response schema (updated May 2026):
#   {
#     "question":        str,
#     "answer":          str | None,   ← None when answer_available=False
#     "answer_available": bool,        ← explicit flag — always check this first
#     "chunks":          [...],        ← ONLY cited chunks (not all retrieved)
#     "cited_chunks":    int,          ← equals len(chunks)
#     "skipped_filters": [str]
#   }
#
# Key changes from old schema:
#   - chunks now contains ONLY chunks the LLM cited when building the answer.
#     Previously it had all retrieved chunks (up to top_k=15), now only cited ones.
#   - answer_available replaces our derived has_results logic
#   - total_chunks removed — use cited_chunks instead
#   - No more rerank_score threshold filtering — cited chunks are already quality-filtered
#   - No more is_general filtering — cited chunks are all relevant by definition
#
# Impact on suggestions:
#   - Fewer chunks BUT higher quality (only cited ones).
#   - Suggestion generation uses file_name + application from cited chunks.
#   - When answer_available=False, chunks=[] → no suggestions → correct behaviour.

import logging
from collections import Counter
from dataclasses import dataclass, field

import httpx

from app.config import settings
from app.exceptions import ExternalServiceError

logger = logging.getLogger(__name__)


@dataclass
class VectorChunk:
    """Single cited chunk from Vector API response."""
    text:       str
    score:      float
    source_url: str
    file_name:  str
    application: str | None
    metadata:   dict = field(default_factory=dict)


@dataclass
class VectorSearchResult:
    """
    Clean result returned to agent tools.

    answer           — LLM-generated answer grounded in cited chunks.
                       None when answer_available is False.

    answer_available — True if knowledge base had enough info to answer.
                       False = out of scope / no relevant documents found.
                       Always check this before reading answer.

    chunks           — Only the chunks cited when building the answer.
                       Empty when answer_available is False.
                       No filtering needed — API already quality-filtered.

    app_identified   — Most frequent app in cited chunks (informational).
                       Used for Phase 2 Dataverse health check hook.

    cited_chunks     — Number of cited chunks. Equals len(chunks).
    """
    answer:           str | None
    answer_available: bool
    chunks:           list[VectorChunk]
    app_identified:   str | None
    cited_chunks:     int


class VectorClient:
    """
    Async HTTP client for Vector API.
    Singleton — initialised once at startup via initialise().
    """

    def __init__(self):
        self._client: httpx.AsyncClient | None = None
        self._initialised: bool = False

    def initialise(self) -> None:
        if not settings.VECTOR_API_URL:
            logger.warning(
                "VECTOR_API_URL not configured — vector search will fail."
            )

        headers = {"Content-Type": "application/json"}
        if settings.VECTOR_API_KEY:
            headers["Authorization"] = (
                f"Bearer {settings.VECTOR_API_KEY.get_secret_value()}"
            )

        self._client = httpx.AsyncClient(
            headers=headers,
            verify=settings.VECTOR_API_VERIFY_SSL,
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=5.0, pool=5.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        self._initialised = True
        logger.info(
            "VectorClient initialised — url=%s ssl_verify=%s",
            settings.VECTOR_API_URL, settings.VECTOR_API_VERIFY_SSL,
        )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            logger.info("VectorClient closed")

    async def search(self, question: str) -> VectorSearchResult:
        if not self._client:
            raise RuntimeError("VectorClient not initialised.")
        if not settings.VECTOR_API_URL:
            raise ExternalServiceError("VectorAPI", "VECTOR_API_URL not configured")

        payload = {"question": question, "top_k": settings.VECTOR_TOP_K}

        try:
            response = await self._client.post(settings.VECTOR_API_URL, json=payload)
            response.raise_for_status()
            data = response.json()

        except httpx.TimeoutException as e:
            logger.error("Vector API timeout: %s", str(e))
            raise ExternalServiceError("VectorAPI", f"Request timed out: {str(e)}")

        except httpx.HTTPStatusError as e:
            logger.error("Vector API HTTP error: %s %s", e.response.status_code, str(e))
            raise ExternalServiceError("VectorAPI", f"HTTP {e.response.status_code}")

        except Exception as e:
            logger.error("Vector API unexpected error: %s", str(e))
            raise ExternalServiceError("VectorAPI", str(e))

        return self._parse_response(data)

    def _parse_response(self, data: dict) -> VectorSearchResult:
        """
        Parses new Vector API response schema.

        Always check answer_available first — it is the authoritative signal.
        When False: answer=None, chunks=[], no sources, no suggestions.
        When True:  answer has content, chunks has only cited documents.
        """
        answer_available = bool(data.get("answer_available", False))
        answer           = data.get("answer") or None
        raw_chunks       = data.get("chunks", [])
        cited_chunks     = data.get("cited_chunks", len(raw_chunks))

        # Parse cited chunks
        # No rerank_score filtering needed — API only returns cited chunks
        # No is_general filtering needed — cited chunks are all relevant
        parsed: list[VectorChunk] = []
        for chunk in raw_chunks:
            metadata   = chunk.get("metadata", {})
            raw_url    = chunk.get("source_url", "")
            source_url = raw_url if raw_url and raw_url.startswith("http") else ""

            parsed.append(VectorChunk(
                text=chunk.get("text", ""),
                score=float(chunk.get("score", 0.0)),
                source_url=source_url,
                file_name=chunk.get("file_name", ""),
                application=metadata.get("application"),
                metadata=metadata,
            ))

        app_identified = self._derive_app(parsed) if parsed else None

        logger.info(
            "Vector search: answer_available=%s cited_chunks=%d app=%s",
            answer_available, cited_chunks, app_identified,
        )

        return VectorSearchResult(
            answer=answer,
            answer_available=answer_available,
            chunks=parsed,
            app_identified=app_identified,
            cited_chunks=cited_chunks,
        )

    def _derive_app(self, chunks: list[VectorChunk]) -> str | None:
        """Derives app_identified from cited chunks — informational only."""
        apps = [c.application for c in chunks if c.application]
        if not apps:
            return None
        most_common, count = Counter(apps).most_common(1)[0]
        return most_common if count >= settings.VECTOR_APP_IDENTIFICATION_MIN_CHUNKS else None


# Singleton
vector_client = VectorClient()