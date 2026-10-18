"""PostgreSQL cache store for Jev classification results."""

import json
import logging
from datetime import datetime

import psycopg

from .classifier import AlertCriticality, CacheStore, ClassificationResult

logger = logging.getLogger(__name__)


class PostgresCacheStore(CacheStore):
    """PostgreSQL implementation of cache store."""

    def __init__(self, database_url: str):
        self.database_url = database_url

    async def get(self, cache_key: str) -> ClassificationResult | None:
        """Get cached classification result."""
        try:
            with psycopg.connect(self.database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT criticality, confidence, probabilities, review_required,
                               source, model_version, classified_at, cost_usd
                        FROM jev_classification_cache
                        WHERE cache_key = %s
                        """,
                        (cache_key,),
                    )
                    row = cur.fetchone()
                    if not row:
                        return None

                    return ClassificationResult(
                        criticality=AlertCriticality(row[0]),
                        confidence=row[1],
                        probabilities=row[2],
                        review_required=row[3],
                        source="cache",  # Override source since it came from cache
                        model_version=row[5],
                        cache_key=cache_key,
                        classified_at=row[6],
                        cost_usd=row[7],
                    )
        except psycopg.Error as exc:
            logger.error("Failed to get cache entry: %s", exc)
            return None

    async def set(self, cache_key: str, result: ClassificationResult) -> None:
        """Cache classification result."""
        try:
            with psycopg.connect(self.database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO jev_classification_cache (
                            cache_key, criticality, confidence, probabilities,
                            review_required, source, model_version, classified_at,
                            cost_usd, request_context
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (cache_key) DO NOTHING
                        """,
                        (
                            cache_key,
                            result.criticality.value,
                            result.confidence,
                            json.dumps(result.probabilities),
                            result.review_required,
                            result.source,
                            result.model_version,
                            result.classified_at,
                            result.cost_usd,
                            json.dumps({"cache_key": cache_key}),  # Placeholder for audit
                        ),
                    )
                conn.commit()
        except psycopg.Error as exc:
            logger.error("Failed to cache classification result: %s", exc)


class InMemoryCacheStore(CacheStore):
    """In-memory cache store for testing."""

    def __init__(self):
        self._cache: dict[str, ClassificationResult] = {}

    async def get(self, cache_key: str) -> ClassificationResult | None:
        """Get cached classification result."""
        result = self._cache.get(cache_key)
        if result:
            # Create a new result with source="cache"
            return ClassificationResult(
                criticality=result.criticality,
                confidence=result.confidence,
                probabilities=result.probabilities,
                review_required=result.review_required,
                source="cache",
                model_version=result.model_version,
                cache_key=result.cache_key,
                classified_at=result.classified_at,
                cost_usd=result.cost_usd,
            )
        return None

    async def set(self, cache_key: str, result: ClassificationResult) -> None:
        """Cache classification result."""
        self._cache[cache_key] = result

    def clear(self):
        """Clear all cache entries (testing only)."""
        self._cache.clear()
