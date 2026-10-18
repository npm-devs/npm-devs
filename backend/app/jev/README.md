# Jev Alert Classification Module

This module provides typed, fast classification of fuel overconsumption alerts using TypeSafe AI's Jev decision model.

## Overview

The Jev module classifies alerts by criticality level (`critical`, `high`, `medium`, `low`) based on anomaly evidence such as overconsumption percentage, idle time, affected segments, and driver reports.

### Key Features

- **Fast**: 70–500ms latency, ~40–200x faster than LLMs
- **Typed**: Always returns one of the defined criticality levels, never free text
- **Calibrated**: Confidence scores are calibrated with RLCD
- **Cost-effective**: ~$0.026 per 1,000 classifications
- **Reproducible**: Hash-based caching ensures same input → same output
- **Resilient**: Automatic retries, deterministic overrides, and fallback classification

## Components

### 1. `JevClient` (client.py)

HTTP client for the Jev Decisions API with retry logic.

**Features**:
- Exponential backoff for transient errors (429, 5xx)
- Retry-After header support
- Handles in-flight budget limits (402)
- Request validation with Pydantic

**Usage**:

```python
from app.jev import JevClient, JevConfig

config = JevConfig.from_env()
async with JevClient(config) as client:
    response = await client.classify(
        state={"alert_context": "..."},
        questions={"criticality": {...}}
    )
    print(response.answers["criticality"].choice)  # "high"
```

### 2. `AlertClassifier` (classifier.py)

High-level classifier with caching and fallback strategies.

**Classification flow**:

1. **Deterministic override**: Clear cases (>50% overconsumption → critical)
2. **Cache lookup**: Check if same evidence was classified before
3. **Jev API call**: Call Jev if cache miss and enabled
4. **Fallback**: Simple rule-based classification if Jev unavailable

**Usage**:

```python
from app.jev import AlertClassifier, AnomalyEvidence
from app.jev.cache import PostgresCacheStore

cache = PostgresCacheStore(database_url)
classifier = AlertClassifier(config, cache)

evidence = AnomalyEvidence(
    vehicle_id="T28",
    trip_id="F-104",
    overconsumption_pct=35.0,
    observed_consumption=43.2,
    baseline_consumption=32.0,
    segments_affected=2,
    distance_km=20.0,
    idle_minutes=40.0,
    driver_report=None,
    trip_status="in_progress",
)

result = await classifier.classify(evidence)
print(result.criticality)  # AlertCriticality.HIGH
print(result.confidence)    # 0.89
print(result.source)        # "jev" | "cache" | "deterministic_override" | "fallback"
```

### 3. `JevConfig` (config.py)

Configuration loaded from environment variables.

**Required**:
- `JEV_API_KEY`: OpenRouter API key (get from https://openrouter.ai/keys)

**Optional**:
- `JEV_API_ENDPOINT`: Decisions API endpoint (default: OpenRouter)
- `JEV_MODEL`: Jev model version (default: `typesafe/jev-1.13.0`)
- `JEV_ENABLED`: Feature flag (default: `false`)
- `JEV_CONFIDENCE_THRESHOLD`: Review threshold (default: `0.70`)
- `JEV_TIMEOUT_SECONDS`: API timeout (default: `10`)
- `JEV_MAX_RETRIES`: Max retry attempts (default: `3`)

### 4. `CacheStore` (cache.py)

Cache interface with PostgreSQL and in-memory implementations.

**PostgresCacheStore**:
- Stores results in `jev_classification_cache` table
- Permanent storage for audit and reproducibility

**InMemoryCacheStore**:
- For testing without database
- Can be cleared with `.clear()`

### 5. Question definitions (questions.py)

Defines the Jev question for criticality classification.

**Components**:
- `JEV_QUESTION_VERSION`: Tracks question changes for cache invalidation
- `JEV_CRITICALITY_QUESTION`: Choice question with 4 criteria
- `build_alert_context()`: Constructs context string from evidence

## Reproducibility Strategy

Jev is **not strictly deterministic**. We achieve consistency through:

### 1. Pinned model version

```python
JEV_MODEL = "typesafe/jev-1.13.0"  # Never use jev-latest
```

### 2. Hash-based cache

```python
cache_key = sha256(json.dumps({
    "vehicle_id": "T28",
    "overconsumption_pct": 35.12,  # Rounded to 2 decimals
    "idle_minutes": 40.7,           # Rounded to 1 decimal
    "model": "typesafe/jev-1.13.0",
    "question_version": "1.0.0",
    ...
}))
```

Same evidence → same cache key → same result from cache.

### 3. Deterministic overrides

Extreme cases bypass Jev:
- >50% overconsumption, no driver report, in progress → `critical`
- >90 min idle, no explanation → `critical`
- <10% overconsumption, single segment → `low`

### 4. Confidence-based review

Low confidence (<0.70) or close probabilities → human review queue.

## Testing

### Run tests

```bash
cd backend
uv run pytest tests/test_jev_classifier.py -v
uv run pytest tests/test_jev_client.py -v
```

### Test without Jev API

Set `JEV_ENABLED=false` in config. Classifier uses fallback automatically.

### Test with mock

```python
from unittest.mock import AsyncMock, Mock

mock_response = Mock()
mock_response.status_code = 200
mock_response.text = '{"model": "...", "answers": {...}, "usage": {...}}'

with patch.object(client.client, "post", AsyncMock(return_value=mock_response)):
    result = await client.classify(state, questions)
```

## Integration Example

```python
# In AlertGenerator.generate_alert():

from app.jev import AlertClassifier, AnomalyEvidence
from app.jev.cache import PostgresCacheStore
from app.jev.config import JevConfig

# Initialize once
jev_config = JevConfig.from_env()
cache_store = PostgresCacheStore(database_url)
classifier = AlertClassifier(jev_config, cache_store)

# For each anomaly
evidence = AnomalyEvidence(
    vehicle_id=anomaly.vehicle_id,
    trip_id=anomaly.trip_id,
    overconsumption_pct=anomaly.overconsumption_pct,
    observed_consumption=anomaly.observed_consumption,
    baseline_consumption=anomaly.baseline_consumption,
    segments_affected=anomaly.segments_affected,
    distance_km=anomaly.distance_km,
    idle_minutes=anomaly.idle_minutes,
    driver_report=anomaly.driver_report,
    trip_status=anomaly.trip_status,
)

classification = await classifier.classify(evidence)

alert = Alert(
    ...
    criticality=classification.criticality,
    confidence=classification.confidence,
    review_required=classification.review_required,
)
```

## Monitoring

### Key metrics

```sql
-- Criticality distribution
SELECT criticality, COUNT(*) 
FROM alerts 
WHERE created_at > NOW() - INTERVAL '24 hours'
GROUP BY criticality;

-- Cache hit rate
SELECT source, COUNT(*) 
FROM jev_classification_cache 
WHERE classified_at > NOW() - INTERVAL '24 hours'
GROUP BY source;

-- Daily cost
SELECT DATE(classified_at), SUM(cost_usd)
FROM jev_classification_cache
WHERE cost_usd IS NOT NULL
GROUP BY DATE(classified_at);

-- Review queue size
SELECT COUNT(*) 
FROM alerts 
WHERE review_required = true AND status = 'pending';
```

### Expected values

- **Cache hit rate**: >80% after 1 week
- **Cost per classification**: <$0.00003
- **Criticality distribution**: ~5–10% critical, ~20–30% high, ~40–50% medium, ~20–30% low
- **Review rate**: <20%

## Calibration

See `docs/jev-implementation-guide.md` for full calibration workflow.

**Quick version**:

1. Export 150 anomalies from real data
2. Label manually with expected criticality
3. Classify with Jev
4. Measure accuracy by confidence band
5. Adjust criteria in `questions.py` if accuracy <85% in high confidence
6. Re-test until calibrated

## Troubleshooting

### "JEV_API_KEY environment variable is required"

Set the env var:

```bash
export JEV_API_KEY=sk-or-v1-...
```

In production, store in GCP Secret Manager.

### "Jev API error 401"

API key is invalid. Check at https://openrouter.ai/keys

### "Jev API error 402: Insufficient credits"

Add credits at https://openrouter.ai/credits

### "Jev API error 429"

Rate limit exceeded. Client retries automatically. If persistent, reduce concurrency.

### Low cache hit rate

Check that `_build_cache_key()` normalizes values correctly. Consider bucketing.

### Accuracy too low

1. Increase sample size (200–500 items)
2. Simplify levels (4 → 3)
3. Make criteria more distinct
4. Get multiple human labels to check inter-rater agreement

## References

- [Jev Classifier Docs](https://www.jevtypesafeai.com/jev/classifier)
- [OpenRouter Jev Cookbook](https://openrouter.ai/docs/cookbook/evaluate-and-optimize/jev-classification)
- [Design doc](../../../docs/jev-alarm-classification.md)
- [Implementation guide](../../../docs/jev-implementation-guide.md)
