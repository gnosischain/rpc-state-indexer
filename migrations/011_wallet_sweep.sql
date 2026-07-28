-- Wallet-interaction discovery sweep: address-less log scans that find every event any
-- contract emitted with a treasury wallet in an indexed topic position. Raw evidence only;
-- measurement targets are admitted from the candidate views, never written here.

CREATE TABLE IF NOT EXISTS {{database}}.sweep_ranges
(
    chain_id UInt64,
    wallet_address String,
    topic_position UInt8,
    range_start_block UInt64,
    range_end_block_exclusive UInt64,
    scan_id UUID,
    status LowCardinality(String),
    anchor_block UInt64,
    anchor_hash String,
    log_count UInt64 DEFAULT 0,
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
    wallet_address,
    topic_position,
    range_start_block,
    range_end_block_exclusive,
    scan_id
);

CREATE TABLE IF NOT EXISTS {{database}}.wallet_interaction_logs
(
    chain_id UInt64,
    wallet_address String,
    topic_position UInt8,
    contract_address String,
    topic0 String,
    topic_count UInt8,
    block_number UInt64,
    block_hash String,
    transaction_hash String,
    log_index UInt64,
    topics Array(String),
    observed_at DateTime64(9, 'UTC') DEFAULT now64(9),
    insert_version UInt64 MATERIALIZED toUInt64(toUnixTimestamp64Nano(now64(9)))
)
ENGINE = ReplacingMergeTree(insert_version)
PARTITION BY tuple()
ORDER BY
(
    chain_id,
    wallet_address,
    topic_position,
    contract_address,
    topic0,
    block_number,
    transaction_hash,
    log_index
);

-- Tokenized candidates: contracts whose sweep hits are receipt-token transfer shapes.
-- ERC20 and ERC721 share the Transfer topic0 and are split by topic arity; the weth9
-- Deposit shape catches direct wraps that emit no Transfer.
CREATE OR REPLACE VIEW {{database}}.v_sweep_candidate_tokens AS
SELECT
    logs.chain_id AS chain_id,
    logs.contract_address AS contract_address,
    multiIf(
        logs.topic0 = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
            AND logs.topic_count = 3, 'erc20',
        logs.topic0 = '0xe1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d3c460751c2402c5c5cc9109c',
            'erc20_weth9',
        logs.topic0 = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
            AND logs.topic_count = 4, 'erc721',
        'erc1155'
    ) AS token_standard,
    count() AS observations,
    uniqExact(logs.wallet_address) AS wallets,
    min(logs.block_number) AS first_seen_block,
    max(logs.block_number) AS last_seen_block
FROM {{database}}.wallet_interaction_logs AS logs
WHERE logs.topic0 IN (
    '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef',
    '0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62',
    '0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb',
    '0xe1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d3c460751c2402c5c5cc9109c'
)
GROUP BY chain_id, contract_address, token_standard;

-- Protocol-interaction candidates: every non-transfer event shape, grouped by contract
-- and signature. This is the evidence queue for keyed-call measurement adapters.
CREATE OR REPLACE VIEW {{database}}.v_sweep_candidate_protocols AS
SELECT
    logs.chain_id AS chain_id,
    logs.contract_address AS contract_address,
    logs.topic0 AS topic0,
    count() AS observations,
    uniqExact(logs.wallet_address) AS wallets,
    min(logs.block_number) AS first_seen_block,
    max(logs.block_number) AS last_seen_block,
    any(logs.transaction_hash) AS example_transaction
FROM {{database}}.wallet_interaction_logs AS logs
WHERE logs.topic0 NOT IN (
    '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef',
    '0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62',
    '0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb',
    '0xe1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d3c460751c2402c5c5cc9109c'
)
GROUP BY chain_id, contract_address, topic0;

-- Sweep health: latest failing ranges (mirrors discovery_ranges diagnostics).
CREATE OR REPLACE VIEW {{database}}.v_sweep_failures AS
SELECT
    ranges.chain_id AS chain_id,
    ranges.wallet_address AS wallet_address,
    ranges.topic_position AS topic_position,
    ranges.range_start_block AS range_start_block,
    ranges.range_end_block_exclusive AS range_end_block_exclusive,
    ranges.error_class AS error_class,
    ranges.error_message AS error_message,
    ranges.started_at AS started_at
FROM {{database}}.sweep_ranges AS ranges
WHERE ranges.status = 'failed';
