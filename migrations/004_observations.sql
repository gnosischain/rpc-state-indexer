CREATE TABLE IF NOT EXISTS {{database}}.token_balances
(
    chain_id UInt64,
    job_name LowCardinality(String),
    token_address String,
    snapshot_date Date,
    attempt_id UUID,
    holder_address String,
    balance_raw UInt256,
    scaled_balance_raw Nullable(UInt256),
    value_kind LowCardinality(String),
    probe_source LowCardinality(String),
    batch_sequence UInt32,
    observed_at DateTime64(9, 'UTC') DEFAULT now64(9),
    insert_version UInt64 MATERIALIZED toUInt64(toUnixTimestamp64Nano(now64(9)))
)
ENGINE = ReplacingMergeTree(insert_version)
PARTITION BY toStartOfMonth(snapshot_date)
ORDER BY
(
    chain_id,
    job_name,
    token_address,
    snapshot_date,
    attempt_id,
    holder_address
);

CREATE TABLE IF NOT EXISTS {{database}}.token_scalars
(
    chain_id UInt64,
    job_name LowCardinality(String),
    token_address String,
    snapshot_date Date,
    attempt_id UUID,
    scalar_name LowCardinality(String),
    scalar_raw UInt256,
    probe_source LowCardinality(String),
    batch_sequence UInt32,
    observed_at DateTime64(9, 'UTC') DEFAULT now64(9),
    insert_version UInt64 MATERIALIZED toUInt64(toUnixTimestamp64Nano(now64(9)))
)
ENGINE = ReplacingMergeTree(insert_version)
PARTITION BY toStartOfMonth(snapshot_date)
ORDER BY
(
    chain_id,
    job_name,
    token_address,
    snapshot_date,
    attempt_id,
    scalar_name
);

CREATE TABLE IF NOT EXISTS {{database}}.pool_token_balances
(
    chain_id UInt64,
    job_name LowCardinality(String),
    pool_address String,
    token_address String,
    snapshot_date Date,
    attempt_id UUID,
    balance_raw UInt256,
    probe_source LowCardinality(String),
    batch_sequence UInt32,
    observed_at DateTime64(9, 'UTC') DEFAULT now64(9),
    insert_version UInt64 MATERIALIZED toUInt64(toUnixTimestamp64Nano(now64(9)))
)
ENGINE = ReplacingMergeTree(insert_version)
PARTITION BY toStartOfMonth(snapshot_date)
ORDER BY
(
    chain_id,
    job_name,
    pool_address,
    snapshot_date,
    attempt_id,
    token_address
);
