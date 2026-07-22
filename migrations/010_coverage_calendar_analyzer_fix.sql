-- Fix v_coverage_calendar for the ClickHouse new analyzer (26.x). Its columns were selected
-- as bare `c.chain_id`/`a.snapshot_date`/... over a 3-way join whose sources all carry
-- chain_id; the analyzer then kept the qualifier in the OUTPUT names (`c.chain_id`, ...), so
-- `SELECT ... FROM v_coverage_calendar WHERE chain_id = ...` failed with Code 47. Views are
-- lazy, so 007 created it fine and it only broke at query time (the `status` command).
-- Fix: alias every projected column to a clean name. See [[clickhouse-analyzer-view-sql]].
CREATE OR REPLACE VIEW {{database}}.v_coverage_calendar AS
SELECT
    c.chain_id AS chain_id,
    c.job_name AS job_name,
    c.target_kind AS target_kind,
    c.target_address AS target_address,
    a.snapshot_date AS snapshot_date,
    c.cadence AS cadence,
    c.integrity_mode AS integrity_mode,
    if(s.signature_count > 1, 'conflict', if(s.signature_count = 1, 'published', 'missing'))
        AS coverage_status,
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
