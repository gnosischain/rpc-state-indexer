-- Token display metadata, observed on-chain at a pinned block.
--
-- This is an OBSERVATION, not configuration: it is read from the contract at an immutable
-- anchor exactly like a balance. Keeping it out of the catalog is deliberate — a token's
-- config_hash must not move when its metadata is resolved later, or every already-published
-- row would drop out of the eligible views. Consumers join on (chain_id, token_address).
--
-- decimals is Nullable and NULL means "not observed". A decimals of 0 is a legitimate
-- on-chain answer for some tokens, so the two must never be conflated: treating unknown as
-- zero silently multiplies a balance by 10^0 and produces a plausible-looking wrong number.

CREATE TABLE IF NOT EXISTS {{database}}.token_metadata
(
    chain_id UInt64,
    token_address String,
    symbol Nullable(String),
    name Nullable(String),
    decimals Nullable(UInt8),
    -- resolved: every field observed. partial: at least one observed. failed: none.
    resolution_status LowCardinality(String),
    -- Encoding actually accepted per field, for auditability: string | bytes32 | absent.
    symbol_encoding LowCardinality(String) DEFAULT '',
    name_encoding LowCardinality(String) DEFAULT '',
    anchor_block UInt64,
    anchor_hash String,
    error_class LowCardinality(String) DEFAULT '',
    error_message String DEFAULT '',
    observed_at DateTime64(9, 'UTC') DEFAULT now64(9),
    insert_version UInt64 MATERIALIZED toUInt64(toUnixTimestamp64Nano(now64(9)))
)
ENGINE = ReplacingMergeTree(insert_version)
PARTITION BY tuple()
ORDER BY (chain_id, token_address);

CREATE OR REPLACE VIEW {{database}}.v_token_metadata_current AS
SELECT
    chain_id,
    token_address,
    symbol,
    name,
    decimals,
    resolution_status,
    symbol_encoding,
    name_encoding,
    anchor_block,
    anchor_hash,
    error_class,
    error_message,
    observed_at
FROM {{database}}.token_metadata FINAL;

-- Consumer contract for treasury dashboards. The raw integer stays authoritative;
-- balance_units is display-only and NULL whenever decimals is unknown, so a caller can
-- never silently render an unscaled number as if it were scaled.
--
-- anchor_block/anchor_hash are carried deliberately: every figure here is attributable to
-- an immutable finalized block, which is the whole point of this indexer over a portfolio API.
CREATE OR REPLACE VIEW {{database}}.v_treasury_balances AS
SELECT
    b.chain_id AS chain_id,
    b.snapshot_date AS snapshot_date,
    b.job_name AS job_name,
    b.holder_address AS wallet_address,
    b.token_address AS token_address,
    m.symbol AS symbol,
    m.decimals AS decimals,
    m.resolution_status AS metadata_status,
    b.balance_raw AS balance_raw,
    if(
        m.decimals IS NULL,
        NULL,
        toFloat64(b.balance_raw) / pow(10, toFloat64(m.decimals))
    ) AS balance_units,
    b.anchor_block AS anchor_block,
    b.anchor_hash AS anchor_hash
FROM {{database}}.v_token_balances_published AS b
LEFT JOIN {{database}}.v_token_metadata_current AS m
       ON b.chain_id = m.chain_id
      AND b.token_address = m.token_address;

-- Which admitted targets still lack metadata (drives the resolve pass and its metrics).
CREATE OR REPLACE VIEW {{database}}.v_token_metadata_gaps AS
SELECT
    cand.chain_id AS chain_id,
    cand.contract_address AS token_address,
    cand.first_seen_block AS first_seen_block,
    meta.resolution_status AS resolution_status
FROM {{database}}.v_sweep_candidate_tokens AS cand
LEFT JOIN {{database}}.v_token_metadata_current AS meta
       ON cand.chain_id = meta.chain_id
      AND cand.contract_address = meta.token_address
WHERE cand.token_standard IN ('erc20', 'erc20_weth9')
  AND (meta.resolution_status IS NULL OR meta.resolution_status != 'resolved');
