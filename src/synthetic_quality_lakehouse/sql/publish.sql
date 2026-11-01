-- Existing dedicated target tables only. All publication and run receipt are atomic.
BEGIN TRANSACTION;
ASSERT (SELECT COUNT(*) FROM `{dataset}.pipeline_runs` WHERE run_id = @run_id) <= 1
  AS 'Duplicate run receipts require investigation';
ASSERT NOT EXISTS (
  SELECT 1 FROM `{dataset}.pipeline_runs`
  WHERE run_id = @run_id AND source_fingerprint <> @fingerprint
) AS 'Run ID already belongs to different source/code';
ASSERT NOT EXISTS (
  SELECT 1 FROM `{dataset}.pipeline_runs` WHERE run_id = @run_id
    AND (source_rows <> @source_rows OR clean_rows <> @clean_rows
      OR quarantined_rows <> @quarantined_rows OR quality_rows <> @quality_rows
      OR source_uri <> @source_uri OR source_generation <> @source_generation
      OR source_sha256 <> @source_sha256)
) AS 'Existing receipt metadata differs';

ASSERT (SELECT COUNT(*) FROM `{ledger}`) = @source_rows AS 'Source count mismatch';
ASSERT (SELECT COUNT(DISTINCT source_row_id) FROM `{ledger}`) = @source_rows
  AS 'Source ledger contains duplicate row IDs';
ASSERT NOT EXISTS (
  SELECT 1 FROM `{ledger}` WHERE run_id <> @run_id OR source_fingerprint <> @fingerprint
    OR disposition NOT IN ('clean', 'quarantine')
) AS 'Unexpected ledger identity or disposition';
ASSERT (SELECT COUNT(*) FROM `{clean}`) = @clean_rows AS 'Clean count mismatch';
ASSERT (SELECT COUNT(*) FROM `{quarantine}`) = @quarantined_rows AS 'Quarantine count mismatch';
ASSERT (SELECT COUNT(*) FROM `{quality}`) = @quality_rows AS 'Quality count mismatch';
ASSERT @clean_rows + @quarantined_rows = @source_rows AS 'Routing counts mismatch';
ASSERT NOT EXISTS (
  SELECT 1 FROM `{clean}` WHERE run_id <> @run_id OR source_fingerprint <> @fingerprint
  UNION ALL
  SELECT 1 FROM `{quarantine}` WHERE run_id <> @run_id OR source_fingerprint <> @fingerprint
  UNION ALL
  SELECT 1 FROM `{quality}` WHERE run_id <> @run_id OR source_fingerprint <> @fingerprint
) AS 'Unexpected staged identity';
ASSERT (SELECT COUNT(DISTINCT check_name) FROM `{quality}`) = @quality_rows
  AS 'Duplicate quality checks';
ASSERT NOT EXISTS (
  SELECT 1 FROM `{quality}`
  WHERE evaluated_rows <> @source_rows OR failed_rows < 0 OR failed_rows > evaluated_rows
     OR passed <> (failed_rows = 0)
) AS 'Invalid quality counters';
ASSERT NOT EXISTS (
  SELECT 1 FROM `{quarantine}`
  WHERE ARRAY_LENGTH(error_codes) = 0
     OR LOWER(TO_HEX(SHA256(raw_payload))) <> source_record_sha256
) AS 'Invalid quarantine evidence';

IF NOT EXISTS (SELECT 1 FROM `{dataset}.pipeline_runs` WHERE run_id = @run_id) THEN
  ASSERT NOT EXISTS (
    SELECT 1 FROM `{dataset}.clean_transactions` WHERE run_id = @run_id
    UNION ALL
    SELECT 1 FROM `{dataset}.quarantined_records` WHERE run_id = @run_id
    UNION ALL
    SELECT 1 FROM `{dataset}.quality_results` WHERE run_id = @run_id
  ) AS 'Uncommitted target rows require investigation';
  INSERT INTO `{dataset}.clean_transactions` SELECT * FROM `{clean}`;
  INSERT INTO `{dataset}.quarantined_records` SELECT * FROM `{quarantine}`;
  INSERT INTO `{dataset}.quality_results` SELECT * FROM `{quality}`;
  INSERT INTO `{dataset}.pipeline_runs`
    VALUES (@run_id, @fingerprint, @source_rows, @clean_rows, @quarantined_rows,
            @quality_rows, @source_uri, @source_generation, @source_sha256, CURRENT_TIMESTAMP());
END IF;

ASSERT (({reconcile}) ) AS 'Source-to-target reconciliation failed';
ASSERT (SELECT COUNT(*) FROM `{dataset}.quality_results` WHERE run_id = @run_id) = @quality_rows
  AS 'Published quality count mismatch';
COMMIT TRANSACTION;
