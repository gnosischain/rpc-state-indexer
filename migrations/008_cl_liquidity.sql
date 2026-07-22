CREATE TABLE IF NOT EXISTS {{database}}.pool_cl_state
(
    chain_id UInt64,
    job_name LowCardinality(String),
    pool_address String,
    snapshot_date Date,
    attempt_id UUID,
    pool_class LowCardinality(String),
    sqrt_price_x96 UInt256,
    current_tick Int32,
    liquidity UInt256,
    fee_growth_global_0_x128 UInt256,
    fee_growth_global_1_x128 UInt256,
    tick_spacing Int32,
    fee UInt32,
    tick_count UInt32,
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
    attempt_id
);

CREATE TABLE IF NOT EXISTS {{database}}.pool_tick_liquidity
(
    chain_id UInt64,
    job_name LowCardinality(String),
    pool_address String,
    snapshot_date Date,
    attempt_id UUID,
    tick Int32,
    liquidity_gross UInt256,
    -- liquidityNet/liquidityDelta is a signed int128 on-chain; keep the sign exactly.
    liquidity_net Int256,
    fee_growth_outside_0_x128 UInt256,
    fee_growth_outside_1_x128 UInt256,
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
    tick
);

-- Published contract: a CL state/tick row is visible only when its attempt won the
-- append-only publication gate (same join as the other v_*_published views).
CREATE OR REPLACE VIEW {{database}}.v_pool_cl_state_published AS
SELECT
    s.chain_id,
    s.job_name,
    s.pool_address,
    s.snapshot_date,
    s.attempt_id,
    s.pool_class,
    s.sqrt_price_x96,
    s.current_tick,
    s.liquidity,
    s.fee_growth_global_0_x128,
    s.fee_growth_global_1_x128,
    s.tick_spacing,
    s.fee,
    s.tick_count,
    s.probe_source,
    s.batch_sequence,
    s.observed_at,
    p.config_hash,
    p.anchor_block,
    p.anchor_hash,
    p.result_digest
FROM (SELECT * FROM {{database}}.pool_cl_state FINAL) AS s
INNER JOIN {{database}}.v_publications_current AS p
    ON s.chain_id = p.chain_id
   AND s.job_name = p.job_name
   AND p.target_kind = 'pool'
   AND s.pool_address = p.target_address
   AND s.snapshot_date = p.snapshot_date
   AND s.attempt_id = p.attempt_id;

CREATE OR REPLACE VIEW {{database}}.v_pool_tick_liquidity_published AS
SELECT
    t.chain_id,
    t.job_name,
    t.pool_address,
    t.snapshot_date,
    t.attempt_id,
    t.tick,
    t.liquidity_gross,
    t.liquidity_net,
    t.fee_growth_outside_0_x128,
    t.fee_growth_outside_1_x128,
    t.probe_source,
    t.batch_sequence,
    t.observed_at,
    p.config_hash,
    p.anchor_block,
    p.anchor_hash,
    p.result_digest
FROM (SELECT * FROM {{database}}.pool_tick_liquidity FINAL) AS t
INNER JOIN {{database}}.v_publications_current AS p
    ON t.chain_id = p.chain_id
   AND t.job_name = p.job_name
   AND p.target_kind = 'pool'
   AND t.pool_address = p.target_address
   AND t.snapshot_date = p.snapshot_date
   AND t.attempt_id = p.attempt_id;
