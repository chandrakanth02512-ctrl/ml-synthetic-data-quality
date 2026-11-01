"""Run-scoped prediction publication, with no updates/deletions or overrides."""

PUBLISH = """
BEGIN TRANSACTION;
ASSERT (SELECT COUNT(*) FROM `{dataset}.model_runs` WHERE run_id = @run_id) <= 1
  AS 'Duplicate model receipts';
ASSERT NOT EXISTS (
  SELECT 1 FROM `{dataset}.model_runs` WHERE run_id = @run_id
    AND (source_fingerprint <> @fingerprint OR model_sha256 <> @model_sha256
      OR training_rows <> @training_rows OR evaluation_rows <> @evaluation_rows)
) AS 'Conflicting model run';
ASSERT (SELECT COUNT(*) FROM `{stage}`) = @evaluation_rows AS 'Prediction count mismatch';
ASSERT (SELECT COUNT(DISTINCT transaction_id) FROM `{stage}`) = @evaluation_rows
  AS 'Duplicate transaction predictions';
ASSERT (SELECT COUNT(DISTINCT source_row_id) FROM `{stage}`) = @evaluation_rows
  AS 'Duplicate source row predictions';
ASSERT NOT EXISTS (
  SELECT 1 FROM `{stage}` WHERE run_id <> @run_id OR source_fingerprint <> @fingerprint
    OR model_sha256 <> @model_sha256 OR IS_NAN(anomaly_score) OR IS_INF(anomaly_score)
    OR rule_based_prediction NOT IN (0,1) OR isolation_forest_prediction NOT IN (0,1)
    OR actual_anomaly NOT IN (0,1)
    OR combined_or_prediction <> GREATEST(rule_based_prediction, isolation_forest_prediction)
) AS 'Invalid prediction identity or detector outputs';
IF NOT EXISTS (SELECT 1 FROM `{dataset}.model_runs` WHERE run_id = @run_id) THEN
  ASSERT NOT EXISTS (SELECT 1 FROM `{dataset}.anomaly_predictions` WHERE run_id = @run_id)
    AS 'Uncommitted predictions require investigation';
  INSERT INTO `{dataset}.anomaly_predictions` SELECT * FROM `{stage}`;
  INSERT INTO `{dataset}.model_runs` VALUES (
    @run_id, @fingerprint, @model_sha256, @training_rows, @evaluation_rows, CURRENT_TIMESTAMP());
END IF;
ASSERT (SELECT COUNT(*) FROM `{dataset}.anomaly_predictions` WHERE run_id = @run_id)
  = @evaluation_rows
  AS 'Published prediction count mismatch';
ASSERT NOT EXISTS (
  (SELECT * FROM `{stage}` EXCEPT DISTINCT
   SELECT * FROM `{dataset}.anomaly_predictions` WHERE run_id = @run_id)
  UNION ALL
  (SELECT * FROM `{dataset}.anomaly_predictions` WHERE run_id = @run_id EXCEPT DISTINCT
   SELECT * FROM `{stage}`)
) AS 'Published predictions differ from immutable source';
COMMIT TRANSACTION;
"""
