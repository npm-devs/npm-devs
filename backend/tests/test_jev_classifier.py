"""Tests for Jev alert classifier."""

import pytest

from app.jev.cache import InMemoryCacheStore
from app.jev.classifier import AlertClassifier, AlertCriticality, AnomalyEvidence
from app.jev.config import JevConfig


@pytest.fixture
def cache_store():
    """In-memory cache store for testing."""
    return InMemoryCacheStore()


@pytest.fixture
def config():
    """Jev config with disabled API (testing without real API calls)."""
    return JevConfig(
        api_key="test_key",
        enabled=False,  # Disabled for tests without mocking
        confidence_threshold=0.70,
    )


@pytest.fixture
def classifier(config, cache_store):
    """Alert classifier instance."""
    return AlertClassifier(config, cache_store)


def test_deterministic_override_critical_high_overconsumption(classifier):
    """Critical override for massive overconsumption without explanation."""
    evidence = AnomalyEvidence(
        vehicle_id="T28",
        trip_id="F-104",
        overconsumption_pct=55.0,  # >50%
        observed_consumption=50.0,
        baseline_consumption=32.0,
        segments_affected=3,
        distance_km=30.0,
        idle_minutes=20.0,
        driver_report=None,  # No explanation
        trip_status="in_progress",
    )

    override = classifier._deterministic_override(evidence)
    assert override == AlertCriticality.CRITICAL


def test_deterministic_override_critical_extreme_idle(classifier):
    """Critical override for extreme idle time."""
    evidence = AnomalyEvidence(
        vehicle_id="T28",
        trip_id="F-104",
        overconsumption_pct=25.0,
        observed_consumption=40.0,
        baseline_consumption=32.0,
        segments_affected=2,
        distance_km=20.0,
        idle_minutes=95.0,  # >90 min
        driver_report=None,
        trip_status="in_progress",
    )

    override = classifier._deterministic_override(evidence)
    assert override == AlertCriticality.CRITICAL


def test_deterministic_override_low_minimal_overconsumption(classifier):
    """Low override for minimal overconsumption in single segment."""
    evidence = AnomalyEvidence(
        vehicle_id="T28",
        trip_id="F-104",
        overconsumption_pct=8.0,  # <10%
        observed_consumption=34.0,
        baseline_consumption=32.0,
        segments_affected=1,  # Single segment
        distance_km=10.0,
        idle_minutes=5.0,
        driver_report=None,
        trip_status="in_progress",
    )

    override = classifier._deterministic_override(evidence)
    assert override == AlertCriticality.LOW


def test_deterministic_override_low_completed_explained(classifier):
    """Low override for completed trip with minor explained overconsumption."""
    evidence = AnomalyEvidence(
        vehicle_id="T28",
        trip_id="F-104",
        overconsumption_pct=18.0,
        observed_consumption=38.0,
        baseline_consumption=32.0,
        segments_affected=2,
        distance_km=20.0,
        idle_minutes=15.0,
        driver_report="Esperando descarga",  # Explained
        trip_status="completed",  # Completed
    )

    override = classifier._deterministic_override(evidence)
    assert override == AlertCriticality.LOW


def test_no_override_for_borderline_case(classifier):
    """No override for borderline case that needs Jev."""
    evidence = AnomalyEvidence(
        vehicle_id="T28",
        trip_id="F-104",
        overconsumption_pct=28.0,  # Between thresholds
        observed_consumption=41.0,
        baseline_consumption=32.0,
        segments_affected=2,
        distance_km=20.0,
        idle_minutes=35.0,
        driver_report=None,
        trip_status="in_progress",
    )

    override = classifier._deterministic_override(evidence)
    assert override is None


@pytest.mark.asyncio
async def test_fallback_classification_critical(classifier):
    """Fallback classification for critical overconsumption."""
    evidence = AnomalyEvidence(
        vehicle_id="T28",
        trip_id="F-104",
        overconsumption_pct=45.0,  # >=40%
        observed_consumption=46.0,
        baseline_consumption=32.0,
        segments_affected=3,
        distance_km=30.0,
        idle_minutes=40.0,
        driver_report=None,
        trip_status="in_progress",
    )

    cache_key = classifier._build_cache_key(evidence)
    result = classifier._fallback_classification(evidence, cache_key)

    assert result.criticality == AlertCriticality.CRITICAL
    assert result.source == "fallback"
    assert result.review_required is True  # Fallback always requires review


@pytest.mark.asyncio
async def test_fallback_classification_high(classifier):
    """Fallback classification for high overconsumption."""
    evidence = AnomalyEvidence(
        vehicle_id="T28",
        trip_id="F-104",
        overconsumption_pct=30.0,  # >=25%
        observed_consumption=42.0,
        baseline_consumption=32.0,
        segments_affected=2,
        distance_km=20.0,
        idle_minutes=30.0,
        driver_report=None,
        trip_status="in_progress",
    )

    cache_key = classifier._build_cache_key(evidence)
    result = classifier._fallback_classification(evidence, cache_key)

    assert result.criticality == AlertCriticality.HIGH
    assert result.source == "fallback"


@pytest.mark.asyncio
async def test_fallback_downgrade_with_driver_report(classifier):
    """Fallback downgrades criticality when driver explains."""
    evidence = AnomalyEvidence(
        vehicle_id="T28",
        trip_id="F-104",
        overconsumption_pct=42.0,  # Would be CRITICAL
        observed_consumption=45.0,
        baseline_consumption=32.0,
        segments_affected=3,
        distance_km=30.0,
        idle_minutes=40.0,
        driver_report="Tráfico pesado en la ruta",  # Explained
        trip_status="in_progress",
    )

    cache_key = classifier._build_cache_key(evidence)
    result = classifier._fallback_classification(evidence, cache_key)

    assert result.criticality == AlertCriticality.HIGH  # Downgraded from CRITICAL


@pytest.mark.asyncio
async def test_cache_hit(classifier, cache_store):
    """Classification uses cached result when available."""
    evidence = AnomalyEvidence(
        vehicle_id="T28",
        trip_id="F-104",
        overconsumption_pct=28.0,
        observed_consumption=41.0,
        baseline_consumption=32.0,
        segments_affected=2,
        distance_km=20.0,
        idle_minutes=35.0,
        driver_report=None,
        trip_status="in_progress",
    )

    # First call - will use fallback (Jev disabled)
    result1 = await classifier.classify(evidence)
    assert result1.source == "fallback"

    # Second call - should hit cache
    result2 = await classifier.classify(evidence)
    assert result2.source == "cache"
    assert result2.criticality == result1.criticality


@pytest.mark.asyncio
async def test_build_cache_key_stability(classifier):
    """Cache key is stable for identical evidence."""
    evidence1 = AnomalyEvidence(
        vehicle_id="T28",
        trip_id="F-104",
        overconsumption_pct=28.123456,  # Precision
        observed_consumption=41.0,
        baseline_consumption=32.0,
        segments_affected=2,
        distance_km=20.0,
        idle_minutes=35.678,  # Precision
        driver_report="test",
        trip_status="in_progress",
    )

    evidence2 = AnomalyEvidence(
        vehicle_id="T28",
        trip_id="F-104",
        overconsumption_pct=28.129999,  # Rounds to same
        observed_consumption=41.0,
        baseline_consumption=32.0,
        segments_affected=2,
        distance_km=20.0,
        idle_minutes=35.649,  # Rounds to same
        driver_report="test",
        trip_status="in_progress",
    )

    key1 = classifier._build_cache_key(evidence1)
    key2 = classifier._build_cache_key(evidence2)

    # Should be same due to rounding (2 decimals for pct, 1 for minutes)
    assert key1 == key2


def test_requires_review_low_confidence(classifier):
    """Review required when confidence is low."""
    assert classifier._requires_review(0.65, {}) is True  # Below 0.70
    assert classifier._requires_review(0.75, {}) is False  # Above 0.70


def test_requires_review_close_probabilities(classifier):
    """Review required when top two probabilities are close."""
    probabilities = {
        "critical": 0.38,
        "high": 0.35,  # Difference 0.03 < 0.15
        "medium": 0.20,
        "low": 0.07,
    }
    assert classifier._requires_review(0.85, probabilities) is True


def test_requires_review_clear_winner(classifier):
    """No review needed when one option dominates."""
    probabilities = {
        "critical": 0.85,
        "high": 0.10,  # Difference 0.75 > 0.15
        "medium": 0.03,
        "low": 0.02,
    }
    assert classifier._requires_review(0.92, probabilities) is False
