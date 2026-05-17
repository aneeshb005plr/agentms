# app/agents/clients/vector_client.py
# HTTP client for XYZ Vector API (knowledge base search).
#
# Confirmed endpoint:
#   POST https://webapp-docassist.east.dev.ngc.XYZinternal.com
#        /api/vector-retrieval/api/v1/nextgenams_dev/query
#
# Request:
#   { "question": str, "top_k": int }
#
# Response:
#   {
#     "question":     str,
#     "answer":       str,       ← we use chunks not this answer
#     "chunks":       [          ← we use these
#       {
#         "text":       str,
#         "score":      float,
#         "source_url": str,
#         "file_name":  str,
#         "metadata": {
#           "application": str,   ← used to derive app_identified
#           "is_general":  bool,  ← general knowledge, not app-specific
#           "rerank_score": float ← quality gate
#           ...
#         }
#       }
#     ],
#     "total_chunks": int
#   }
#
# Design decisions:
#   - Use chunks not vector API answer — we control tone and quality
#   - Filter by rerank_score >= VECTOR_RERANK_SCORE_THRESHOLD
#   - app_identified = most frequent app in top chunks (informational only)
#   - is_general chunks always included — apply across all apps
#   - Returns VectorSearchResult — clean structure for agent tools
#   - httpx.AsyncClient — singleton created at startup via initialise()
#   - Timeout configured explicitly — never rely on defaults

import logging
from dataclasses import dataclass, field
from collections import Counter

import httpx

from app.config import settings
from app.exceptions import ExternalServiceError

logger = logging.getLogger(__name__)


@dataclass
class VectorChunk:
    """Single chunk from Vector API response."""
    text:         str
    score:        float
    rerank_score: float
    source_url:   str
    file_name:    str
    application:  str | None
    is_general:   bool
    metadata:     dict = field(default_factory=dict)


@dataclass
class VectorSearchResult:
    """
    Clean result returned to agent tools.

    chunks         — filtered chunks above rerank_score threshold
    app_identified — most frequent app in top chunks (informational only)
                     None if mixed apps or no clear winner
    has_results    — False when all chunks below threshold = no info found
    total_chunks   — raw count from API (before filtering)
    """
    chunks:         list[VectorChunk]
    app_identified: str | None
    has_results:    bool
    total_chunks:   int


class VectorClient:
    """
    Async HTTP client for Vector API.
    Singleton — initialised once at startup via initialise().
    httpx.AsyncClient reused across requests — connection pooling.
    """

    def __init__(self):
        self._client: httpx.AsyncClient | None = None
        self._initialised: bool = False

    def initialise(self) -> None:
        """
        Creates httpx.AsyncClient singleton.
        Called once in FastAPI lifespan startup.
        """
        if not settings.VECTOR_API_URL:
            logger.warning(
                "VECTOR_API_URL not configured — vector search will fail. "
                "Set it in .env or secrets volume."
            )

        headers = {"Content-Type": "application/json"}

        # Add API key if configured
        if settings.VECTOR_API_KEY:
            headers["Authorization"] = f"Bearer {settings.VECTOR_API_KEY.get_secret_value()}"

        self._client = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(
                connect=5.0,
                read=30.0,   # vector search can be slow — 30s read timeout
                write=5.0,
                pool=5.0,
            ),
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
            ),
        )
        self._initialised = True
        logger.info("VectorClient initialised — url=%s", settings.VECTOR_API_URL)

    async def close(self) -> None:
        """Called at FastAPI shutdown to close httpx client cleanly."""
        if self._client:
            await self._client.aclose()
            logger.info("VectorClient closed")

    async def search(self, question: str) -> VectorSearchResult:
        """
        Searches knowledge base via Vector API.

        Args:
            question: user question or rewritten query

        Returns:
            VectorSearchResult with filtered chunks and app_identified

        Raises:
            ExternalServiceError if API call fails
        """
        if not self._client:
            raise RuntimeError("VectorClient not initialised. Call initialise() first.")

        if not settings.VECTOR_API_URL:
            raise ExternalServiceError("VectorAPI", "VECTOR_API_URL not configured")

        payload = {
            "question": question,
            "top_k":    settings.VECTOR_TOP_K,
        }

        try:
            response = await self._client.post(
                settings.VECTOR_API_URL,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

        except httpx.TimeoutException as e:
            logger.error("Vector API timeout: %s", str(e))
            raise ExternalServiceError("VectorAPI", f"Request timed out: {str(e)}")

        except httpx.HTTPStatusError as e:
            logger.error("Vector API HTTP error: %s %s", e.response.status_code, str(e))
            raise ExternalServiceError(
                "VectorAPI",
                f"HTTP {e.response.status_code}: {str(e)}"
            )

        except Exception as e:
            logger.error("Vector API unexpected error: %s", str(e))
            raise ExternalServiceError("VectorAPI", str(e))

        return self._parse_response(data)

    def _parse_response(self, data: dict) -> VectorSearchResult:
        """
        Parses Vector API response into VectorSearchResult.

        Steps:
        1. Parse all chunks
        2. Filter by rerank_score >= VECTOR_RERANK_SCORE_THRESHOLD
        3. Derive app_identified from top chunks
        """
        raw_chunks = data.get("chunks", [])
        total_chunks = data.get("total_chunks", len(raw_chunks))

        # Parse all chunks
        parsed = []
        for chunk in raw_chunks:
            metadata     = chunk.get("metadata", {})
            rerank_score = float(metadata.get("rerank_score", 0.0))
            parsed.append(VectorChunk(
                text=chunk.get("text", ""),
                score=float(chunk.get("score", 0.0)),
                rerank_score=rerank_score,
                source_url=chunk.get("source_url", ""),
                file_name=chunk.get("file_name", ""),
                application=metadata.get("application"),
                is_general=bool(metadata.get("is_general", False)),
                metadata=metadata,
            ))

        # Filter by rerank_score threshold
        filtered = [
            c for c in parsed
            if c.rerank_score >= settings.VECTOR_RERANK_SCORE_THRESHOLD
        ]

        has_results = len(filtered) > 0

        # Derive app_identified from filtered chunks
        app_identified = self._derive_app(filtered)

        if not has_results:
            logger.info(
                "Vector search: no chunks above threshold %.2f — no relevant info found",
                settings.VECTOR_RERANK_SCORE_THRESHOLD,
            )
        else:
            logger.info(
                "Vector search: %d/%d chunks above threshold | app=%s",
                len(filtered), total_chunks, app_identified,
            )

        return VectorSearchResult(
            chunks=filtered,
            app_identified=app_identified,
            has_results=has_results,
            total_chunks=total_chunks,
        )

    def _derive_app(self, chunks: list[VectorChunk]) -> str | None:
        """
        Derives app_identified from top chunks.

        Rules:
        - Exclude is_general=True chunks — they are not app-specific
        - Count app occurrences across remaining chunks
        - If most frequent app appears >= VECTOR_APP_IDENTIFICATION_MIN_CHUNKS
          times → that is app_identified
        - Otherwise → None (mixed results, no clear winner)

        This is informational only — used for Phase 2 Dataverse health check.
        Never used to filter chunks.
        """
        if not chunks:
            return None

        # Only count app-specific chunks (not general knowledge)
        app_chunks = [c for c in chunks if not c.is_general and c.application]
        if not app_chunks:
            return None

        app_counts = Counter(c.application for c in app_chunks)
        most_common_app, count = app_counts.most_common(1)[0]

        if count >= settings.VECTOR_APP_IDENTIFICATION_MIN_CHUNKS:
            return most_common_app

        return None


# Singleton
vector_client = VectorClient()