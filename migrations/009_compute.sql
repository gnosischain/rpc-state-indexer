-- Layer 2 derived table. NOT an RPC observation and NOT behind the publication gate: it is
-- deterministically recomputed from the published CL primitives and inherits verification
-- through its provenance columns (source_attempt_id + source_result_digest).
CREATE TABLE IF NOT EXISTS {{database}}.pool_liquidity_profile
(
    chain_id UInt64,
    pool_address String,
    snapshot_date Date,
    tick_lower Int32,
    tick_upper Int32,
    active_liquidity UInt256,
    source_attempt_id UUID,
    source_result_digest String,
    computed_at DateTime64(9, 'UTC') DEFAULT now64(9),
    insert_version UInt64 MATERIALIZED toUInt64(toUnixTimestamp64Nano(now64(9)))
)
ENGINE = ReplacingMergeTree(insert_version)
PARTITION BY toStartOfMonth(snapshot_date)
ORDER BY
(
    chain_id,
    pool_address,
    snapshot_date,
    tick_lower
);

CREATE OR REPLACE VIEW {{database}}.v_pool_liquidity_profile AS
SELECT * FROM {{database}}.pool_liquidity_profile FINAL;
