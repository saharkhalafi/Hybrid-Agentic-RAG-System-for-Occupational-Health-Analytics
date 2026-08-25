"""Gemini API embedding service.

Uses the Gemini Developer API directly instead of Vertex AI.

Model:
    gemini-embedding-001

Authentication:
    GEMINI_API_KEY

Endpoint:
    https://generativelanguage.googleapis.com/v1beta/models/
    gemini-embedding-001:embedContent
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

import requests

from config.logging import get_logger
from config.settings import get_settings

logger = get_logger(__name__)

_shared_embedding_service: "EmbeddingService | None" = None


def get_embedding_service() -> "EmbeddingService":
    """Return process-wide singleton embedding service."""

    global _shared_embedding_service

    if _shared_embedding_service is None:
        _shared_embedding_service = EmbeddingService()

    return _shared_embedding_service


class EmbeddingService:
    """Gemini Developer API embedding service."""

    def __init__(self) -> None:
        self.settings = get_settings()

        self.api_key = self.settings.gemini_api_key

        self.base_url = self.settings.gemini_api_url.rstrip("/")

        self._session: requests.Session | None = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def dimension(self) -> int:
        return self.settings.vector_dimension

    @property
    def model_name(self) -> str:
        return self.settings.embedding_model

    @property
    def endpoint(self) -> str:
        return (
            f"{self.base_url}/models/"
            f"{self.model_name}:embedContent"
        )

    # ------------------------------------------------------------------
    # HTTP session
    # ------------------------------------------------------------------

    def _get_session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()

            self._session.headers.update(
                {
                    "Content-Type": "application/json",
                }
            )

        return self._session

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    def available(self) -> bool:
        """
        Check whether the service is configured.

        This does not make a network request.
        """

        return bool(
            self.api_key
            and self.model_name
            and self.base_url
        )

    # ------------------------------------------------------------------
    # Single embedding
    # ------------------------------------------------------------------

    def _embed_one(self, text: str) -> list[float]:
        """Generate one embedding using Gemini API."""

        if not self.api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not configured."
            )

        payload = {
            "model": f"models/{self.model_name}",
            "content": {
                "parts": [
                    {
                        "text": text,
                    }
                ]
            },
            "outputDimensionality": self.dimension,
        }

        response = self._get_session().post(
            self.endpoint,
            params={
                "key": self.api_key,
            },
            json=payload,
            timeout=self.settings.embedding_timeout_seconds,
        )

        if not response.ok:
            try:
                error_body = response.json()
            except Exception:
                error_body = response.text

            logger.error(
                "gemini_embedding_api_error",
                status_code=response.status_code,
                model=self.model_name,
                error=error_body,
            )

            response.raise_for_status()

        data = response.json()

        embedding = data.get("embedding")

        if not embedding:
            raise RuntimeError(
                "Gemini API response did not contain an embedding."
            )

        values = embedding.get("values")

        if not values:
            raise RuntimeError(
                "Gemini API response contained an empty embedding."
            )

        values = list(values)

        if len(values) != self.dimension:
            logger.warning(
                "embedding_dimension_mismatch",
                expected=self.dimension,
                got=len(values),
                model=self.model_name,
            )

        return values

    # ------------------------------------------------------------------
    # Batch embedding
    # ------------------------------------------------------------------

    def _embed_batch(
        self,
        texts: list[str],
    ) -> list[list[float]]:
        """
        Generate embeddings for multiple texts.

        Gemini embedContent accepts one content item per request,
        so requests are performed independently.
        """

        if not texts:
            return []

        results: list[list[float]] = []

        for text in texts:
            results.append(
                self._embed_one(text)
            )

        return results

    # ------------------------------------------------------------------
    # Public embedding API
    # ------------------------------------------------------------------

    def embed_texts(
        self,
        texts: list[str],
    ) -> list[list[float]]:
        """
        Generate embeddings with cache support.
        """

        if not texts:
            return []

        from cache.ttl_cache import get_embedding_cache
        from api.errors import EmbeddingTimeoutError

        settings = self.settings

        cache = get_embedding_cache(
            max_size=settings.embedding_cache_max_size,
            ttl_seconds=settings.embedding_cache_ttl_seconds,
        )

        results: list[list[float] | None] = [
            None
        ] * len(texts)

        uncached_indices: list[int] = []
        uncached_texts: list[str] = []

        # --------------------------------------------------------------
        # Cache lookup
        # --------------------------------------------------------------

        for i, text in enumerate(texts):

            cached = cache.get(text)

            if cached is not None:

                try:
                    from observability.embed_stats import (
                        record_cache_hit,
                    )

                    record_cache_hit(text)

                except Exception:
                    pass

                results[i] = cached

            else:

                uncached_indices.append(i)
                uncached_texts.append(text)

        # --------------------------------------------------------------
        # Nothing left to embed
        # --------------------------------------------------------------

        if not uncached_texts:
            return [
                r
                for r in results
                if r is not None
            ]

        # --------------------------------------------------------------
        # Cache misses
        # --------------------------------------------------------------

        try:
            from observability.embed_stats import (
                record_cache_miss,
            )

            for text in uncached_texts:
                record_cache_miss(text)

        except Exception:
            pass

        # --------------------------------------------------------------
        # API availability
        # --------------------------------------------------------------

        if not self.available():
            raise RuntimeError(
                "Gemini embedding service unavailable. "
                "Check GEMINI_API_KEY and embedding configuration."
            )

        # --------------------------------------------------------------
        # API call with timeout
        # --------------------------------------------------------------

        timeout = settings.embedding_timeout_seconds

        with ThreadPoolExecutor(
            max_workers=1
        ) as pool:

            future = pool.submit(
                self._embed_batch,
                uncached_texts,
            )

            try:

                new_embeddings = future.result(
                    timeout=timeout,
                )

            except FuturesTimeoutError as exc:

                raise EmbeddingTimeoutError(
                    timeout_seconds=timeout
                ) from exc

        # --------------------------------------------------------------
        # Validate result count
        # --------------------------------------------------------------

        if len(new_embeddings) != len(
            uncached_texts
        ):
            raise RuntimeError(
                f"Expected "
                f"{len(uncached_texts)} embeddings, "
                f"got {len(new_embeddings)}"
            )

        # --------------------------------------------------------------
        # Store cache
        # --------------------------------------------------------------

        for (
            idx,
            text,
            embedding,
        ) in zip(
            uncached_indices,
            uncached_texts,
            new_embeddings,
        ):

            cache.put(
                text,
                embedding,
            )

            results[idx] = embedding

        # --------------------------------------------------------------
        # Return preserving input order
        # --------------------------------------------------------------

        return [
            r
            for r in results
            if r is not None
        ]

    # ------------------------------------------------------------------
    # Convenience method
    # ------------------------------------------------------------------

    def embed(
        self,
        text: str,
    ) -> list[float]:
        """Generate one embedding."""

        results = self.embed_texts(
            [text]
        )

        if not results:
            raise RuntimeError(
                "No embedding returned."
            )

        return results[0]