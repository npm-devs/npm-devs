# Jev Integration Summary

## Overview

This PR adds support for **Jev (TypeSafe AI's typed classification model)** to classify fuel overconsumption alerts by criticality level. The integration is designed for **maximum reproducibility** given Jev's consistency characteristics.

## Deliverables

### 1. Design Document

📄 **`docs/jev-alarm-classification.md`**

Comprehensive design covering:

- **What is Jev**: Decision model optimized for classification (70–500ms latency, $0.042/1M tokens input)
- **Determinism limits**: Not strictly deterministic; probabilities drift ±0.01–0.03, borderline items occasionally flip
- **Current alert flow**: How anomalies flow from detection → alert → WhatsApp
- **Integration point**: Between anomaly detection and alert generation
- **Criticality levels**: `critical`, `high`, `medium`, `low` with specific criteria
- **Reproducibility strategy**:
  - Pinned model version (`jev-1.13.0`)
  - Hash-based cache (PostgreSQL)
  - Deterministic overrides for clear cases
  - Confidence-based review queue
  - Versioned questions and criteria
- **Architecture**: Components, data models, cost/latency estimates
- **Anti-spam policy**: Different throttle windows per criticality
- **Monitoring**: Metrics, calibration process, risks

### 2. Example Code

📁 **`backend/app/jev/`**

Production-ready implementation following repo conventions:

#### Components

| File | Purpose |
|------|---------|
| `client.py` | HTTP client for Jev Decisions API with retry logic |
| `classifier.py` | High-level classifier with caching and fallbacks |
| `config.py` | Configuration from environment variables |
| `questions.py` | Question definitions and context builder |
| `cache.py` | PostgreSQL and in-memory cache stores |
| `README.md` | Module documentation |

#### Key Features

✅ **API key from env var** (`JEV_API_KEY`), never hardcoded  
✅ **Feature flag** (`JEV_ENABLED=false` by default)  
✅ **Caching** with hash-based keys for reproducibility  
✅ **Deterministic overrides** for obvious cases (>50% overconsumption → critical)  
✅ **Fallback classification** when Jev is disabled or unavailable  
✅ **Confidence thresholds** for human review queue  
✅ **Retry with exponential backoff** for transient errors  
✅ **Comprehensive tests** with mocks (no live API calls needed)

#### Database Schema

📄 **`backend/schema/jev_cache.sql`**

```sql
CREATE TABLE jev_classification_cache (
    cache_key TEXT PRIMARY KEY,
    criticality TEXT NOT NULL,
    confidence NUMERIC(5,4),
    probabilities JSONB NOT NULL,
    review_required BOOLEAN NOT NULL,
    source TEXT NOT NULL,  -- 'jev', 'cache', 'deterministic_override', 'fallback'
    model_version TEXT NOT NULL,
    classified_at TIMESTAMPTZ NOT NULL,
    cost_usd NUMERIC(12,10),
    ...
);
```

#### Tests

📁 **`backend/tests/`**

- `test_jev_client.py`: Client with mocked HTTP responses
- `test_jev_classifier.py`: Classifier logic (deterministic overrides, cache, fallback)

All tests run without live API calls.

### 3. Implementation Guide

📄 **`docs/jev-implementation-guide.md`**

Step-by-step instructions for deployment:

1. **Get OpenRouter API key** (https://openrouter.ai/keys)
2. **Configure env vars** (local `.env`, GCP Secret Manager)
3. **Create cache table** (`schema/jev_cache.sql`)
4. **Integrate in alert flow** (modify `AlertGenerator`)
5. **Test with mocks** (no live API needed)
6. **Calibrate with labeled sample** (100–200 anomalies)
7. **Adjust questions** based on accuracy (target: >85% in high confidence)
8. **Enable feature flag** (`JEV_ENABLED=true`)
9. **Monitor in production** (metrics, costs, review queue)
10. **Rollback if needed** (disable flag instantly)

Includes:

- **Troubleshooting** section for common issues
- **Calibration scripts** (label sample, classify, evaluate, confusion matrix)
- **SQL queries** for monitoring
- **Rollback procedure**

## How Alarms Flow in This Project

### Current State (MVP in progress)

```
Telemetry (FEAD replay) 
  → Fuel consumption by segment
  → Compare with baseline (peers in same segment & direction)
  → Overconsumption detected
  → Anomaly registered (groups consecutive segments)
  → Alert created
  → WhatsApp notification
```

**Data model** (from issues #14, #15):

- **Telemetría**: GPS + fuel (L/h, L accumulated)
- **Línea base**: Expected consumption per segment & direction (calculated from peers, days 1–12)
- **Sobreconsumo**: Consumption above baseline beyond threshold
- **Anomalía**: Finding that groups multiple segments with overconsumption
- **Reporte del conductor**: WhatsApp messages from driver (e.g., "waiting for unloading")
- **Alerta**: Notification to fleet manager, with evidence

**Problem**: No criticality classification. All alerts treated equally.

### With Jev Integration

```
Anomaly registered
  ↓
  ├─ Evidence: segments, consumption, idle, driver report
  ├─ Deterministic override? (>50% → critical)
  ├─ Cache hit? (same evidence before)
  ├─ Jev API call (if enabled, not in cache)
  └─ Fallback (if Jev unavailable)
  ↓
Alert with criticality (critical/high/medium/low)
  ↓
Anti-spam by level (critical: no throttle, low: 6h window)
  ↓
WhatsApp with emoji (🚨 critical, ⚠️ high, ⚡ medium, ℹ️ low)
```

## Where Jev Plugs In

**Integration point**: After `Anomaly` is registered, before `Alert` is sent.

```python
# In AlertGenerator.generate_alert(anomaly):

evidence = AnomalyEvidence(
    vehicle_id=anomaly.vehicle_id,
    overconsumption_pct=anomaly.overconsumption_pct,
    idle_minutes=anomaly.idle_minutes,
    driver_report=anomaly.driver_report,
    ...
)

classification = await jev_classifier.classify(evidence)

alert = Alert(
    ...
    criticality=classification.criticality,  # NEW
    confidence=classification.confidence,      # NEW
    review_required=classification.review_required,  # NEW
)
```

## Criticality Levels Chosen

Based on transportation fuel management domain:

| Level | Criteria | Action |
|-------|----------|--------|
| **Critical** | >40% overconsumption without explanation, or >60min idle without justification, in progress | Review immediately; call driver |
| **High** | 25–40% overconsumption without clear explanation, or 30–60min idle, or >3 segments affected | Review in <1 hour; prepare action |
| **Medium** | 15–25% overconsumption, or explained by driver ("waiting for unloading"), or <30min idle | Monitor; review after trip |
| **Low** | <15% overconsumption, or single segment, or known cause (steep slope) | Log only; no immediate action |

**Thresholds** (%, minutes) will be calibrated against real FEAD replay data.

## Determinism Approach

Jev is **not strictly deterministic**. Our strategy:

### 1. Pinned model version

```python
JEV_MODEL = "typesafe/jev-1.13.0"  # Never jev-latest
```

### 2. Hash-based cache

```python
cache_key = sha256({
    "vehicle_id": "T28",
    "overconsumption_pct": round(35.123, 2),  # Normalize to 2 decimals
    "idle_minutes": round(40.67, 1),           # Normalize to 1 decimal
    "model": "jev-1.13.0",
    "question_version": "1.0.0",
    ...
})
```

**Same evidence → same cache key → same result** (from cache, not re-classified).

### 3. Deterministic overrides

Clear cases bypass Jev:

- >50% overconsumption, no report, in progress → `critical`
- >90min idle, no explanation → `critical`
- <10% overconsumption, single segment → `low`

### 4. Confidence-based review

- Confidence <0.70 → review queue
- Top two probabilities very close (<0.15 diff) → review queue

### 5. Versioned questions

```python
JEV_QUESTION_VERSION = "1.0.0"
```

Increment when criteria change → invalidates old cache.

**Result**: Same anomaly almost always gets same classification. Borderline cases with low confidence go to human review.

## What We Couldn't Verify

### Jev API Details

✅ **Verified from official docs**:
- Endpoint: `https://openrouter.ai/api/alpha/decisions`
- Model: `typesafe/jev-1.13.0`
- Pricing: $0.042/1M input tokens, output free
- Latency: 70–500ms
- Response structure: `{model, answers, usage}`
- Confidence calibration with RLCD
- Not strictly deterministic (probabilities drift, borderline items flip)

❓ **Assumptions / Not verified**:
- Actual response times under load (expect 70–500ms per docs)
- Rate limits without paid plan (client handles 429s)
- Whether model version stays available long-term (TypeSafe commits to stability)
- Exact conditions when probabilities flip (docs say "borderline cases, ~2 of 20")

### Integration Assumptions

The example code assumes:

1. **Anomaly model exists** with fields: `vehicle_id`, `trip_id`, `overconsumption_pct`, `idle_minutes`, `driver_report`, etc. (from issue #15)
2. **Alert model can be extended** with `criticality`, `confidence`, `review_required` fields
3. **PostgreSQL database** is available (per ADR 0003)
4. **AlertGenerator exists** and can be modified to call Jev classifier

These are **reasonable assumptions** based on the product context and current architecture, but implementation may need adjustment to match actual code structure once issues #14 and #15 are complete.

## Open Questions for the User

1. **4 levels or 3?**  
   Should `medium` and `low` be merged into a single "non-urgent" level if the distinction isn't actionable?

2. **Re-classify on new driver report?**  
   If an anomaly classified as `high` gets a driver report later ("waiting for unloading"), should it be automatically re-classified to `medium`?

3. **Dashboard integration?**  
   Show alerts filtered by criticality with color badges? (Suggested: P1, after MVP)

4. **Self-hosted Jev?**  
   TypeSafe offers self-hosting for enterprise. Worth evaluating after MVP proves value?

5. **Other criticality factors?**  
   Include freight value, cargo type (perishable), driver history? (Suggested: Phase 2)

6. **Calibration sample size?**  
   100–200 anomalies enough, or aim for 500+ for better confidence?

---

## Cost Estimate

At ~250 tokens per alert context:

- **Per classification**: ~$0.0000105 ($0.042/1M × 250)
- **1,000 alerts**: ~$0.01
- **10,000 alerts/month**: ~$0.11

**Cache reduces real cost**: After 1 week, expect >80% cache hit rate → ~$0.02/month for 10k alerts.

**Very low cost**. Main expense is developer time for calibration.

## Testing Without Live API

All tests run with mocks. No API key needed for testing:

```bash
cd backend
uv run pytest tests/test_jev_client.py -v
uv run pytest tests/test_jev_classifier.py -v
```

Set `JEV_ENABLED=false` to test full flow with fallback classification.

## Next Steps

1. **Review this PR**: Design doc, code, implementation guide
2. **Decide**: Do we want to proceed with Jev integration?
3. **If yes**:
   - Get OpenRouter API key
   - Integrate after issue #15 (alerts) is complete
   - Calibrate with 150 real anomalies from FEAD replay
   - Enable in production with `JEV_ENABLED=true`
4. **If no**: Close PR, but design doc and reproducibility strategy may be useful for future ML classification

---

**PR Status**: DRAFT - Ready for review  
**Questions?**: See implementation guide troubleshooting section or open questions above
