-- Jev classification cache table
-- Stores classification results to ensure reproducibility and reduce API costs

CREATE TABLE IF NOT EXISTS jev_classification_cache (
    cache_key TEXT PRIMARY KEY,
    anomaly_id UUID,  -- Optional: link to anomaly if exists
    criticality TEXT NOT NULL,
    confidence NUMERIC(5,4),  -- NULL if deterministic override
    probabilities JSONB NOT NULL DEFAULT '{}',
    review_required BOOLEAN NOT NULL DEFAULT false,
    source TEXT NOT NULL,  -- 'jev', 'cache', 'deterministic_override', 'fallback'
    model_version TEXT NOT NULL,
    classified_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    cost_usd NUMERIC(12,10),  -- Track Jev API cost
    request_context JSONB NOT NULL DEFAULT '{}',  -- Audit trail
    
    CONSTRAINT criticality_valid CHECK (criticality IN ('critical', 'high', 'medium', 'low')),
    CONSTRAINT source_valid CHECK (source IN ('jev', 'cache', 'deterministic_override', 'fallback'))
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_jev_cache_anomaly ON jev_classification_cache(anomaly_id) WHERE anomaly_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_jev_cache_classified_at ON jev_classification_cache(classified_at);
CREATE INDEX IF NOT EXISTS idx_jev_cache_criticality ON jev_classification_cache(criticality);
CREATE INDEX IF NOT EXISTS idx_jev_cache_review ON jev_classification_cache(review_required) WHERE review_required = true;

-- Comments
COMMENT ON TABLE jev_classification_cache IS 'Cache of Jev alert criticality classifications for reproducibility and cost reduction';
COMMENT ON COLUMN jev_classification_cache.cache_key IS 'SHA-256 hash of normalized request context (vehicle, trip, evidence, model version, question version)';
COMMENT ON COLUMN jev_classification_cache.criticality IS 'Classification result: critical, high, medium, or low';
COMMENT ON COLUMN jev_classification_cache.confidence IS 'Jev confidence score (0-1), or NULL if deterministic override';
COMMENT ON COLUMN jev_classification_cache.probabilities IS 'Probability distribution across all criticality levels';
COMMENT ON COLUMN jev_classification_cache.review_required IS 'True if confidence is below threshold or probabilities are close';
COMMENT ON COLUMN jev_classification_cache.source IS 'Classification source: jev API, cache hit, deterministic rule, or fallback';
COMMENT ON COLUMN jev_classification_cache.model_version IS 'Jev model version used (e.g., typesafe/jev-1.13.0)';
COMMENT ON COLUMN jev_classification_cache.cost_usd IS 'Jev API call cost in USD (from usage.cost), NULL for non-API sources';
COMMENT ON COLUMN jev_classification_cache.request_context IS 'Original request context for audit and debugging';
