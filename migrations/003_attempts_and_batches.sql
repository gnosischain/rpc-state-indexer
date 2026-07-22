CREATE TABLE IF NOT EXISTS {{database}}.census_attempts
(
    chain_id UInt64,
    job_name LowCardinality(String),
    target_kind LowCardinality(String),
    target_address String,
    snapshot_date Date,
    attempt_id UUID,
    status LowCardinality(String),
    integrity_mode LowCardinality(String),
    config_hash FixedString(64),
    anchor_block UInt64,
    anchor_hash String,
    executor_kind LowCardinality(String),
    block_reference_kind LowCardinality(String),
    universe_hash Nullable(FixedString(64)),
    universe_size UInt64 DEFAULT 0,
    batches_total UInt32 DEFAULT 0,
    batches_verified UInt32 DEFAULT 0,
    observations_ok UInt64 DEFAULT 0,
    observations_failed UInt64 DEFAULT 0,
    result_digest Nullable(FixedString(64)),
    batches_json String DEFAULT '[]',
    error_class LowCardinality(String) DEFAULT '',
    error_message String DEFAULT '',
    started_at DateTime64(9, 'UTC'),
    heartbeat_at DateTime64(9, 'UTC'),
    finished_at Nullable(DateTime64(9, 'UTC')),
    insert_version UInt64 MATERIALIZED toUInt64(toUnixTimestamp64Nano(now64(9)))
)
ENGINE = ReplacingMergeTree(insert_version)
PARTITION BY toStartOfMonth(snapshot_date)
ORDER BY
(
    chain_id,
    job_name,
    target_kind,
    target_address,
    snapshot_date,
    attempt_id
);

CREATE TABLE IF NOT EXISTS {{database}}.census_universe_members
(
    chain_id UInt64,
    job_name LowCardinality(String),
    target_kind LowCardinality(String),
    target_address String,
    snapshot_date Date,
    attempt_id UUID,
    holder_address String,
    member_sources Array(String),
    member_ordinal UInt64,
    inserted_at DateTime64(9, 'UTC') DEFAULT now64(9),
    insert_version UInt64 MATERIALIZED toUInt64(toUnixTimestamp64Nano(now64(9)))
)
ENGINE = ReplacingMergeTree(insert_version)
PARTITION BY toStartOfMonth(snapshot_date)
ORDER BY
(
    chain_id,
    job_name,
    target_kind,
    target_address,
    snapshot_date,
    attempt_id,
    holder_address
);

CREATE TABLE IF NOT EXISTS {{database}}.census_errors
(
    chain_id UInt64,
    job_name LowCardinality(String),
    target_kind LowCardinality(String),
    target_address String,
    snapshot_date Date,
    attempt_id UUID,
    subject_address String,
    call_kind LowCardinality(String),
    batch_sequence UInt32,
    error_class LowCardinality(String),
    rpc_code Nullable(Int32),
    return_data String DEFAULT '',
    error_message String,
    terminal_at DateTime64(9, 'UTC') DEFAULT now64(9),
    insert_version UInt64 MATERIALIZED toUInt64(toUnixTimestamp64Nano(now64(9)))
)
ENGINE = ReplacingMergeTree(insert_version)
PARTITION BY toStartOfMonth(snapshot_date)
ORDER BY
(
    chain_id,
    job_name,
    target_kind,
    target_address,
    snapshot_date,
    attempt_id,
    subject_address,
    call_kind
);
