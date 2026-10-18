"""Alert criticality classifier using Jev."""

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from .client import JevApiError, JevClient
from .config import JevConfig
from .questions import JEV_CRITICALITY_QUESTION, JEV_QUESTION_VERSION, build_alert_context

logger = logging.getLogger(__name__)


class AlertCriticality(str, Enum):
    """Alert criticality levels."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class AnomalyEvidence:
    """Evidence for an anomaly used in classification."""

    vehicle_id: str
    trip_id: str
    overconsumption_pct: float
    observed_consumption: float
    baseline_consumption: float
    segments_affected: int
    distance_km: float
    idle_minutes: float
    driver_report: str | None
    trip_status: str  # "in_progress" or "completed"
    remaining_distance_km: float | None = None


@dataclass
class ClassificationResult:
    """Result of Jev classification."""

    criticality: AlertCriticality
    confidence: float | None  # None if deterministic override
    probabilities: dict[str, float]
    review_required: bool
    source: str  # "jev", "cache", "deterministic_override", "fallback"
    model_version: str
    cache_key: str
    classified_at: datetime
    cost_usd: float | None = None


class AlertClassifier:
    """
    Classify alert criticality using Jev with caching and fallbacks.

    Implements multiple layers of consistency:
    1. Deterministic overrides for clear cases
    2. Cache lookup by request hash
    3. Jev API call
    4. Fallback to rule-based classification
    """

    def __init__(self, config: JevConfig, cache_store: "CacheStore"):
        self.config = config
        self.cache = cache_store
        self.client = JevClient(config) if config.enabled else None

    async def classify(self, evidence: AnomalyEvidence) -> ClassificationResult:
        """
        Classify an anomaly by criticality.

        Args:
            evidence: Anomaly evidence

        Returns:
            ClassificationResult with criticality and metadata
        """
        # 1. Build cache key
        cache_key = self._build_cache_key(evidence)

        # 2. Check for deterministic override
        override = self._deterministic_override(evidence)
        if override:
            logger.info(
                "Using deterministic override for %s: %s", evidence.vehicle_id, override.value
            )
            return ClassificationResult(
                criticality=override,
                confidence=None,
                probabilities={},
                review_required=False,
                source="deterministic_override",
                model_version=self.config.model,
                cache_key=cache_key,
                classified_at=datetime.now(UTC),
            )

        # 3. Check cache
        cached = await self.cache.get(cache_key)
        if cached:
            logger.info("Cache hit for %s: %s", evidence.vehicle_id, cached.criticality.value)
            return cached

        # 4. Call Jev if enabled
        if self.config.enabled and self.client:
            try:
                result = await self._classify_with_jev(evidence, cache_key)
                # Save to cache
                await self.cache.set(cache_key, result)
                return result
            except JevApiError as exc:
                logger.error("Jev API error: %s. Falling back to rule-based.", exc)
                # Fall through to fallback

        # 5. Fallback to simple rule-based classification
        logger.warning("Using fallback rule-based classification for %s", evidence.vehicle_id)
        return self._fallback_classification(evidence, cache_key)

    async def _classify_with_jev(
        self, evidence: AnomalyEvidence, cache_key: str
    ) -> ClassificationResult:
        """Classify using Jev API."""
        context = build_alert_context(
            vehicle_id=evidence.vehicle_id,
            trip_id=evidence.trip_id,
            overconsumption_pct=evidence.overconsumption_pct,
            observed_consumption=evidence.observed_consumption,
            baseline_consumption=evidence.baseline_consumption,
            segments_affected=evidence.segments_affected,
            distance_km=evidence.distance_km,
            idle_minutes=evidence.idle_minutes,
            driver_report=evidence.driver_report,
            trip_status=evidence.trip_status,
            remaining_distance_km=evidence.remaining_distance_km,
        )

        state = {"alert_context": context}
        questions = {"criticality": JEV_CRITICALITY_QUESTION}

        response = await self.client.classify(state, questions)

        answer = response.answers["criticality"]
        criticality = AlertCriticality(answer.choice)
        confidence = answer.confidence or 0.0
        probabilities = answer.probabilities

        review_required = self._requires_review(confidence, probabilities)

        logger.info(
            "Jev classified %s as %s (confidence=%.2f, review=%s)",
            evidence.vehicle_id,
            criticality.value,
            confidence,
            review_required,
        )

        return ClassificationResult(
            criticality=criticality,
            confidence=confidence,
            probabilities=probabilities,
            review_required=review_required,
            source="jev",
            model_version=response.model,
            cache_key=cache_key,
            classified_at=datetime.now(UTC),
            cost_usd=response.usage.cost,
        )

    def _deterministic_override(self, evidence: AnomalyEvidence) -> AlertCriticality | None:
        """
        Apply deterministic rules for obvious cases.

        Returns criticality if rule applies, None otherwise.
        """
        # Critical: massive overconsumption without explanation, in progress
        if (
            evidence.overconsumption_pct > 50
            and not evidence.driver_report
            and evidence.trip_status == "in_progress"
        ):
            return AlertCriticality.CRITICAL

        # Critical: extreme idle without justification
        if evidence.idle_minutes > 90 and not evidence.driver_report:
            return AlertCriticality.CRITICAL

        # Low: minimal overconsumption in single segment
        if evidence.overconsumption_pct < 10 and evidence.segments_affected == 1:
            return AlertCriticality.LOW

        # Low: completed trip with explained minor overconsumption
        if (
            evidence.trip_status == "completed"
            and evidence.driver_report
            and evidence.overconsumption_pct < 20
        ):
            return AlertCriticality.LOW

        return None

    def _requires_review(self, confidence: float, probabilities: dict[str, float]) -> bool:
        """
        Determine if classification requires human review.

        Criteria:
        - Low confidence (< threshold)
        - Probabilities very close between top two options (< 0.15 difference)
        """
        if confidence < self.config.confidence_threshold:
            return True

        # Check if top two probabilities are very close
        sorted_probs = sorted(probabilities.values(), reverse=True)
        if len(sorted_probs) >= 2:
            top_two_diff = sorted_probs[0] - sorted_probs[1]
            if top_two_diff < 0.15:
                return True

        return False

    def _fallback_classification(
        self, evidence: AnomalyEvidence, cache_key: str
    ) -> ClassificationResult:
        """
        Simple rule-based classification fallback when Jev is unavailable.
        """
        # Simple thresholds based on overconsumption percentage
        if evidence.overconsumption_pct >= 40:
            criticality = AlertCriticality.CRITICAL
        elif evidence.overconsumption_pct >= 25:
            criticality = AlertCriticality.HIGH
        elif evidence.overconsumption_pct >= 15:
            criticality = AlertCriticality.MEDIUM
        else:
            criticality = AlertCriticality.LOW

        # Downgrade if trip is completed or explained
        if evidence.trip_status == "completed" or evidence.driver_report:
            if criticality == AlertCriticality.CRITICAL:
                criticality = AlertCriticality.HIGH
            elif criticality == AlertCriticality.HIGH:
                criticality = AlertCriticality.MEDIUM

        return ClassificationResult(
            criticality=criticality,
            confidence=None,
            probabilities={},
            review_required=True,  # Always mark fallback for review
            source="fallback",
            model_version="rule-based-v1",
            cache_key=cache_key,
            classified_at=datetime.now(UTC),
        )

    def _build_cache_key(self, evidence: AnomalyEvidence) -> str:
        """
        Build deterministic cache key from evidence.

        Normalizes numeric values to avoid float drift.
        """
        normalized = {
            "vehicle_id": evidence.vehicle_id,
            "trip_id": evidence.trip_id,
            "overconsumption_pct": round(evidence.overconsumption_pct, 2),
            "idle_minutes": round(evidence.idle_minutes, 1),
            "segments_affected": evidence.segments_affected,
            "driver_report": evidence.driver_report or "",
            "trip_status": evidence.trip_status,
            "model": self.config.model,
            "question_version": JEV_QUESTION_VERSION,
        }
        serialized = json.dumps(normalized, sort_keys=True)
        return hashlib.sha256(serialized.encode()).hexdigest()


class CacheStore:
    """
    Abstract cache store interface.

    Implement with PostgreSQL, Redis, or in-memory for testing.
    """

    async def get(self, cache_key: str) -> ClassificationResult | None:
        """Get cached classification result."""
        raise NotImplementedError

    async def set(self, cache_key: str, result: ClassificationResult) -> None:
        """Cache classification result."""
        raise NotImplementedError
