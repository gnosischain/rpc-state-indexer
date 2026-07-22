CREATE TABLE IF NOT EXISTS {{database}}.discovery_ranges
(
    chain_id UInt64,
    token_address String,
    topic0 String,
    range_start_block UInt64,
    range_end_block_exclusive UInt64,
    scan_id UUID,
    status LowCardinality(String),
    anchor_block UInt64,
    anchor_hash String,
    log_count UInt64 DEFAULT 0,
    holder_count UInt64 DEFAULT 0,
    attempt_count UInt16 DEFAULT 1,
    endpoint_fingerprint FixedString(64),
    error_class LowCardinality(String) DEFAULT '',
    error_message String DEFAULT '',
    started_at DateTime64(9, 'UTC'),
    heartbeat_at DateTime64(9, 'UTC'),
    finished_at Nullable(DateTime64(9, 'UTC')),
    insert_version UInt64 MATERIALIZED toUInt64(toUnixTimestamp64Nano(now64(9)))
)
ENGINE = ReplacingMergeTree(insert_version)
PARTITION BY tuple()
ORDER BY
(
    chain_id,
    token_address,
    topic0,
    range_start_block,
    range_end_block_exclusive,
    scan_id
);

CREATE TABLE IF NOT EXISTS {{database}}.holder_universe
(
    chain_id UInt64,
    token_address String,
    holder_address String,
    source LowCardinality(String),
    source_detail String DEFAULT '',
    first_seen_block SimpleAggregateFunction(min, UInt64),
    last_seen_block SimpleAggregateFunction(max, UInt64),
    observations SimpleAggregateFunction(sum, UInt64)
)
ENGINE = AggregatingMergeTree
PARTITION BY tuple()
ORDER BY
(
    chain_id,
    token_address,
    holder_address,
    source,
    source_detail
);
