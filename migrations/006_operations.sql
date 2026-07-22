CREATE TABLE IF NOT EXISTS {{database}}.writer_heartbeats
(
    chain_id UInt64,
    process_id UUID,
    operation LowCardinality(String),
    hostname String,
    details_json String DEFAULT '{}',
    started_at DateTime64(9, 'UTC'),
    heartbeat_at DateTime64(9, 'UTC'),
    insert_version UInt64 MATERIALIZED toUInt64(toUnixTimestamp64Nano(now64(9)))
)
ENGINE = ReplacingMergeTree(insert_version)
PARTITION BY tuple()
ORDER BY (chain_id, process_id);
