CREATE TABLE IF NOT EXISTS {{database}}.census_publications
(
    chain_id UInt64,
    job_name LowCardinality(String),
    target_kind LowCardinality(String),
    target_address String,
    snapshot_date Date,
    publication_id UUID DEFAULT generateUUIDv4(),
    attempt_id UUID,
    executor_kind LowCardinality(String),
    block_reference_kind LowCardinality(String),
    integrity_mode LowCardinality(String),
    config_hash FixedString(64),
    anchor_block UInt64,
    anchor_hash String,
    universe_hash FixedString(64),
    universe_size UInt64,
    result_digest FixedString(64),
    observed_sum_raw Nullable(UInt256),
    reference_supply_raw Nullable(UInt256),
    batches_total UInt32,
    observations_total UInt64,
    provider_groups Array(String),
    checks_passed Array(String),
    published_at DateTime64(9, 'UTC') DEFAULT now64(9)
)
ENGINE = MergeTree
PARTITION BY toStartOfMonth(snapshot_date)
ORDER BY
(
    chain_id,
    job_name,
    target_kind,
    target_address,
    snapshot_date,
    published_at,
    publication_id
);
