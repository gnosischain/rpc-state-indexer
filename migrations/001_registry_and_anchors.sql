CREATE TABLE IF NOT EXISTS {{database}}.config_registry
(
    chain_id UInt64,
    job_name LowCardinality(String),
    target_kind LowCardinality(String),
    target_address String,
    cadence LowCardinality(String),
    integrity_mode LowCardinality(String),
    coverage_start Nullable(Date),
    coverage_end Nullable(Date),
    config_hash FixedString(64),
    canonical_config_json String,
    enabled UInt8 DEFAULT 1,
    registered_at DateTime64(9, 'UTC') DEFAULT now64(9),
    insert_version UInt64 MATERIALIZED toUInt64(toUnixTimestamp64Nano(now64(9)))
)
ENGINE = ReplacingMergeTree(insert_version)
PARTITION BY tuple()
ORDER BY (chain_id, job_name, target_kind, target_address);

CREATE TABLE IF NOT EXISTS {{database}}.day_anchors
(
    chain_id UInt64,
    snapshot_date Date,
    resolution_id UUID DEFAULT generateUUIDv4(),
    block_number UInt64,
    block_hash String,
    parent_hash String,
    block_timestamp DateTime64(0, 'UTC'),
    next_block_number UInt64,
    next_block_hash String,
    next_block_timestamp DateTime64(0, 'UTC'),
    finalized_at_resolution UInt8,
    resolution_source LowCardinality(String),
    endpoint_fingerprint FixedString(64),
    resolved_at DateTime64(9, 'UTC') DEFAULT now64(9)
)
ENGINE = MergeTree
PARTITION BY toStartOfYear(snapshot_date)
ORDER BY (chain_id, snapshot_date, resolved_at, resolution_id);
