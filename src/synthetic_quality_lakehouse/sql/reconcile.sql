-- Run-scoped row identity, disposition, payload hash and amount reconciliation.
WITH targets AS (
  SELECT source_row_id, source_record_sha256, source_fingerprint,
         'clean' AS disposition, transaction_amount_usd AS amount_usd
  FROM `{dataset}.clean_transactions` WHERE run_id = @run_id
  UNION ALL
  SELECT source_row_id, source_record_sha256, source_fingerprint,
         'quarantine' AS disposition, CAST(NULL AS FLOAT64) AS amount_usd
  FROM `{dataset}.quarantined_records` WHERE run_id = @run_id
), source AS (
  SELECT * FROM `{ledger}` WHERE run_id = @run_id
), mismatches AS (
  SELECT s.source_row_id AS source_id, t.source_row_id AS target_id
  FROM source s FULL OUTER JOIN targets t USING (source_row_id)
  WHERE s.source_row_id IS NULL OR t.source_row_id IS NULL
     OR s.source_record_sha256 <> t.source_record_sha256
     OR s.source_fingerprint <> t.source_fingerprint
     OR s.disposition <> t.disposition
     OR ABS(COALESCE(s.amount_usd, 0) - COALESCE(t.amount_usd, 0)) > 0.000001
)
SELECT
  (SELECT COUNT(*) FROM source) AS source_rows,
  (SELECT COUNT(*) FROM targets) AS target_rows,
  (SELECT COUNT(*) - COUNT(DISTINCT source_row_id) FROM source) AS source_duplicates,
  (SELECT COUNT(*) - COUNT(DISTINCT source_row_id) FROM targets) AS target_duplicates,
  (SELECT COUNT(*) FROM mismatches) AS mismatched_rows
