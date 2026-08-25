"""Vertex AI Gemini embedding service (Phase B)."""

from __future__ import annotations

from config.genai_client import create_genai_client
from config.logging import get_logger
from config.settings import get_settings

logger = get_logger(__name__)

_shared_embedding_service: "EmbeddingService | None" = None


def get_embedding_service() -> "EmbeddingService":
    """Process-wide singleton — reuses Vertex AI client across requests."""
    global _shared_embedding_service
    if _shared_embedding_service is None:
        _shared_embedding_service = EmbeddingService()
    return _shared_embedding_service


class EmbeddingService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._client = None

    @property
    def dimension(self) -> int:
        return self.settings.vector_dimension

    @property
    def model_name(self) -> str:
        return self.settings.embedding_model

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            self._client = create_genai_client(self.settings)
            return self._client
        except Exception as exc:
            logger.warning("embedding_client_init_failed", error=str(exc))
            return None

    def available(self) -> bool:
        return self._get_client() is not None

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings via Vertex AI Gemini embedding model (with cache)."""
        if not texts:
            return []

        from cache.ttl_cache import get_embedding_cache
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

        from api.errors import EmbeddingTimeoutError

        settings = self.settings
        cache = get_embedding_cache(
            max_size=settings.embedding_cache_max_size,
            ttl_seconds=settings.embedding_cache_ttl_seconds,
        )

        results: list[list[float] | None] = [None] * len(texts)
        uncached_indices: list[int] = []
        uncached_texts: list[str] = []

        for i, text in enumerate(texts):
            cached = cache.get(text)
            if cached is not None:
                from observability.embed_stats import record_cache_hit
                record_cache_hit(text)
                results[i] = cached
            else:
                uncached_indices.append(i)
                uncached_texts.append(text)

        if uncached_texts:
            from observability.embed_stats import record_cache_miss
            for t in uncached_texts:
                record_cache_miss(t)
            client = self._get_client()
            if client is None:
                raise RuntimeError("Gemini embedding client unavailable")

            from google.genai import types

            config_kwargs: dict = {}
            if self.dimension:
                config_kwargs["output_dimensionality"] = self.dimension

            def _call_embed() -> list[list[float]]:
                response = client.models.embed_content(
                    model=self.model_name,
                    contents=uncached_texts,
                    config=types.EmbedContentConfig(**config_kwargs),
                )
                new_embeddings: list[list[float]] = []
                for emb in response.embeddings or []:
                    values = emb.values or []
                    if len(values) != self.dimension:
                        logger.warning(
                            "embedding_dimension_mismatch",
                            expected=self.dimension,
                            got=len(values),
                            model=self.model_name,
                        )
                    new_embeddings.append(list(values))
                if len(new_embeddings) != len(uncached_texts):
                    raise RuntimeError(f"Expected {len(uncached_texts)} embeddings, got {len(new_embeddings)}")
                return new_embeddings

            timeout = settings.embedding_timeout_seconds
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(_call_embed)
                try:
                    new_embeddings = future.result(timeout=timeout)
                except FuturesTimeoutError as exc:
                    raise EmbeddingTimeoutError(timeout_seconds=timeout) from exc

            for idx, text, emb in zip(uncached_indices, uncached_texts, new_embeddings):
                cache.put(text, emb)
                results[idx] = emb

        return [r for r in results if r is not None]
