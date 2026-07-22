CREATE OR REPLACE VIEW {{database}}.v_config_registry_current AS
SELECT
    chain_id,
    job_name,
    target_kind,
    target_address,
    cadence,
    integrity_mode,
    coverage_start,
    coverage_end,
    config_hash,
    canonical_config_json,
    enabled,
    registered_at
FROM
(
    SELECT
        chain_id,
        job_name,
        target_kind,
        target_address,
        argMax(cr.cadence, tuple(cr.insert_version, cr.config_hash)) AS cadence,
        argMax(cr.integrity_mode, tuple(cr.insert_version, cr.config_hash)) AS integrity_mode,
        tupleElement(
            argMax(tuple(cr.coverage_start), tuple(cr.insert_version, cr.config_hash)),
            1
        ) AS coverage_start,
        tupleElement(
            argMax(tuple(cr.coverage_end), tuple(cr.insert_version, cr.config_hash)),
            1
        ) AS coverage_end,
        argMax(cr.config_hash, tuple(cr.insert_version, cr.config_hash)) AS config_hash,
        argMax(cr.canonical_config_json, tuple(cr.insert_version, cr.config_hash)) AS canonical_config_json,
        argMax(cr.enabled, tuple(cr.insert_version, cr.config_hash)) AS enabled,
        argMax(cr.registered_at, tuple(cr.insert_version, cr.config_hash)) AS registered_at
    FROM {{database}}.config_registry AS cr
    GROUP BY chain_id, job_name, target_kind, target_address
)
WHERE enabled = 1;

CREATE OR REPLACE VIEW {{database}}.v_day_anchor_status AS
SELECT
    chain_id,
    snapshot_date,
    uniqExact(tuple(
        da.block_number,
        da.block_hash,
        da.parent_hash,
        da.block_timestamp,
        da.next_block_number,
        da.next_block_hash,
        da.next_block_timestamp
    )) AS resolution_count,
    argMax(da.block_number, tuple(da.resolved_at, toString(da.resolution_id))) AS block_number,
    argMax(da.block_hash, tuple(da.resolved_at, toString(da.resolution_id))) AS block_hash,
    argMax(da.parent_hash, tuple(da.resolved_at, toString(da.resolution_id))) AS parent_hash,
    argMax(da.block_timestamp, tuple(da.resolved_at, toString(da.resolution_id))) AS block_timestamp,
    argMax(da.next_block_number, tuple(da.resolved_at, toString(da.resolution_id))) AS next_block_number,
    argMax(da.next_block_hash, tuple(da.resolved_at, toString(da.resolution_id))) AS next_block_hash,
    argMax(da.next_block_timestamp, tuple(da.resolved_at, toString(da.resolution_id))) AS next_block_timestamp,
    min(da.finalized_at_resolution) AS all_resolutions_finalized,
    max(da.resolved_at) AS last_resolved_at
FROM {{database}}.day_anchors AS da
GROUP BY chain_id, snapshot_date;

CREATE OR REPLACE VIEW {{database}}.v_day_anchors_canonical AS
SELECT
    chain_id,
    snapshot_date,
    block_number,
    block_hash,
    parent_hash,
    block_timestamp,
    next_block_number,
    next_block_hash,
    next_block_timestamp,
    all_resolutions_finalized,
    last_resolved_at
FROM {{database}}.v_day_anchor_status
WHERE resolution_count = 1
  AND all_resolutions_finalized = 1;

CREATE OR REPLACE VIEW {{database}}.v_anchor_conflicts AS
SELECT *
FROM {{database}}.v_day_anchor_status
WHERE resolution_count > 1;

CREATE OR REPLACE VIEW {{database}}.v_census_attempts_current AS
SELECT * FROM {{database}}.census_attempts FINAL;

CREATE OR REPLACE VIEW {{database}}.v_census_errors_current AS
SELECT * FROM {{database}}.census_errors FINAL;

CREATE OR REPLACE VIEW {{database}}.v_publications_eligible AS
SELECT
    p.chain_id AS chain_id,
    p.job_name AS job_name,
    p.target_kind AS target_kind,
    p.target_address AS target_address,
    p.snapshot_date AS snapshot_date,
    p.publication_id AS publication_id,
    p.attempt_id AS attempt_id,
    p.executor_kind AS executor_kind,
    p.block_reference_kind AS block_reference_kind,
    p.integrity_mode AS integrity_mode,
    p.config_hash AS config_hash,
    p.anchor_block AS anchor_block,
    p.anchor_hash AS anchor_hash,
    p.universe_hash AS universe_hash,
    p.universe_size AS universe_size,
    p.result_digest AS result_digest,
    p.observed_sum_raw AS observed_sum_raw,
    p.reference_supply_raw AS reference_supply_raw,
    p.batches_total AS batches_total,
    p.observations_total AS observations_total,
    p.provider_groups AS provider_groups,
    p.checks_passed AS checks_passed,
    p.published_at AS published_at
FROM {{database}}.census_publications AS p
INNER JOIN {{database}}.v_config_registry_current AS c
    ON p.chain_id = c.chain_id
   AND p.job_name = c.job_name
   AND p.target_kind = c.target_kind
   AND p.target_address = c.target_address
   AND p.config_hash = c.config_hash
INNER JOIN {{database}}.v_day_anchors_canonical AS a
    ON p.chain_id = a.chain_id
   AND p.snapshot_date = a.snapshot_date
   AND p.anchor_block = a.block_number
   AND p.anchor_hash = a.block_hash
INNER JOIN {{database}}.v_census_attempts_current AS t
    ON p.chain_id = t.chain_id
   AND p.job_name = t.job_name
   AND p.target_kind = t.target_kind
   AND p.target_address = t.target_address
   AND p.snapshot_date = t.snapshot_date
   AND p.attempt_id = t.attempt_id
   AND t.status = 'verified'
   AND p.integrity_mode = t.integrity_mode
   AND p.config_hash = t.config_hash
   AND p.anchor_block = t.anchor_block
   AND p.anchor_hash = t.anchor_hash
   AND p.universe_hash = t.universe_hash
   AND p.universe_size = t.universe_size
   AND p.result_digest = t.result_digest
   AND p.batches_total = t.batches_total
   AND t.batches_total = t.batches_verified
   AND p.observations_total = t.observations_ok
   AND t.observations_failed = 0
WHERE p.attempt_id NOT IN
(
    SELECT attempt_id
    FROM {{database}}.v_census_errors_current
);

CREATE OR REPLACE VIEW {{database}}.v_publication_status AS
SELECT
    pub.chain_id,
    pub.job_name,
    pub.target_kind,
    pub.target_address,
    pub.snapshot_date,
    count() AS publication_rows,
    uniqExact(tuple(pub.anchor_hash, pub.config_hash, pub.universe_hash, pub.result_digest)) AS signature_count,
    argMax(pub.attempt_id, tuple(pub.published_at, toString(pub.publication_id))) AS selected_attempt_id,
    argMax(pub.executor_kind, tuple(pub.published_at, toString(pub.publication_id))) AS executor_kind,
    argMax(pub.block_reference_kind, tuple(pub.published_at, toString(pub.publication_id))) AS block_reference_kind,
    argMax(pub.integrity_mode, tuple(pub.published_at, toString(pub.publication_id))) AS integrity_mode,
    argMax(pub.config_hash, tuple(pub.published_at, toString(pub.publication_id))) AS config_hash,
    argMax(pub.anchor_block, tuple(pub.published_at, toString(pub.publication_id))) AS anchor_block,
    argMax(pub.anchor_hash, tuple(pub.published_at, toString(pub.publication_id))) AS anchor_hash,
    argMax(pub.universe_hash, tuple(pub.published_at, toString(pub.publication_id))) AS universe_hash,
    argMax(pub.universe_size, tuple(pub.published_at, toString(pub.publication_id))) AS universe_size,
    argMax(pub.result_digest, tuple(pub.published_at, toString(pub.publication_id))) AS result_digest,
    max(pub.published_at) AS published_at
FROM {{database}}.v_publications_eligible AS pub
GROUP BY pub.chain_id, pub.job_name, pub.target_kind, pub.target_address, pub.snapshot_date;

CREATE OR REPLACE VIEW {{database}}.v_publication_conflicts AS
SELECT *
FROM {{database}}.v_publication_status
WHERE signature_count > 1;

CREATE OR REPLACE VIEW {{database}}.v_publications_current AS
SELECT
    chain_id,
    job_name,
    target_kind,
    target_address,
    snapshot_date,
    selected_attempt_id AS attempt_id,
    executor_kind,
    block_reference_kind,
    integrity_mode,
    config_hash,
    anchor_block,
    anchor_hash,
    universe_hash,
    universe_size,
    result_digest,
    published_at
FROM {{database}}.v_publication_status
WHERE signature_count = 1;

CREATE OR REPLACE VIEW {{database}}.v_token_balances_published AS
SELECT
    b.chain_id,
    b.job_name,
    b.token_address,
    b.snapshot_date,
    b.attempt_id,
    b.holder_address,
    b.balance_raw,
    b.scaled_balance_raw,
    b.value_kind,
    b.probe_source,
    b.batch_sequence,
    b.observed_at,
    p.config_hash,
    p.anchor_block,
    p.anchor_hash,
    p.universe_hash,
    p.universe_size,
    p.result_digest
FROM (SELECT * FROM {{database}}.token_balances FINAL) AS b
INNER JOIN {{database}}.v_publications_current AS p
    ON b.chain_id = p.chain_id
   AND b.job_name = p.job_name
   AND p.target_kind = 'token'
   AND b.token_address = p.target_address
   AND b.snapshot_date = p.snapshot_date
   AND b.attempt_id = p.attempt_id;

CREATE OR REPLACE VIEW {{database}}.v_token_scalars_published AS
SELECT
    s.chain_id,
    s.job_name,
    s.token_address,
    s.snapshot_date,
    s.attempt_id,
    s.scalar_name,
    s.scalar_raw,
    s.probe_source,
    s.batch_sequence,
    s.observed_at,
    p.config_hash,
    p.anchor_block,
    p.anchor_hash,
    p.result_digest
FROM (SELECT * FROM {{database}}.token_scalars FINAL) AS s
INNER JOIN {{database}}.v_publications_current AS p
    ON s.chain_id = p.chain_id
   AND s.job_name = p.job_name
   AND p.target_kind = 'token'
   AND s.token_address = p.target_address
   AND s.snapshot_date = p.snapshot_date
   AND s.attempt_id = p.attempt_id;

CREATE OR REPLACE VIEW {{database}}.v_pool_token_balances_published AS
SELECT
    b.chain_id,
    b.job_name,
    b.pool_address,
    b.token_address,
    b.snapshot_date,
    b.attempt_id,
    b.balance_raw,
    b.probe_source,
    b.batch_sequence,
    b.observed_at,
    p.config_hash,
    p.anchor_block,
    p.anchor_hash,
    p.result_digest
FROM (SELECT * FROM {{database}}.pool_token_balances FINAL) AS b
INNER JOIN {{database}}.v_publications_current AS p
    ON b.chain_id = p.chain_id
   AND b.job_name = p.job_name
   AND p.target_kind = 'pool'
   AND b.pool_address = p.target_address
   AND b.snapshot_date = p.snapshot_date
   AND b.attempt_id = p.attempt_id;

CREATE OR REPLACE VIEW {{database}}.v_coverage_calendar AS
SELECT
    c.chain_id,
    c.job_name,
    c.target_kind,
    c.target_address,
    a.snapshot_date,
    c.cadence,
    c.integrity_mode,
    if(s.signature_count > 1, 'conflict', if(s.signature_count = 1, 'published', 'missing')) AS coverage_status,
    s.selected_attempt_id AS attempt_id,
    s.published_at AS published_at
FROM {{database}}.v_config_registry_current AS c
INNER JOIN {{database}}.v_day_anchors_canonical AS a
    ON c.chain_id = a.chain_id
LEFT JOIN {{database}}.v_publication_status AS s
    ON c.chain_id = s.chain_id
   AND c.job_name = s.job_name
   AND c.target_kind = s.target_kind
   AND c.target_address = s.target_address
   AND a.snapshot_date = s.snapshot_date
WHERE c.cadence = 'daily'
  AND (isNull(c.coverage_start) OR a.snapshot_date >= c.coverage_start)
  AND (isNull(c.coverage_end) OR a.snapshot_date < c.coverage_end);
