# Walk through 10 verified cloud records

These are **VERIFIED CLOUD ARTIFACT EXPORTS** from the synthetic
`workflow-smoke-002` evaluation split, not a new local model run. Open the CSV
files on GitHub to browse them as tables. No GCP access is needed.

The same 10 transaction IDs appear in the input, cleaned output and predictions.
The selection contains all five actual anomalies, all three Isolation Forest
false positives and two normal records. It deliberately overrepresents unusual
cases; it is not a random or representative sample of the full dataset.

## Follow the data

1. Start with [input_transactions.csv](input_transactions.csv): seven input
   fields, including a synthetic customer ID, amount, currency and UTC timestamp.
   There are no anomaly labels in these transaction inputs.
2. Compare [cleaned_transactions.csv](cleaned_transactions.csv). Spark retained
   the valid records, normalized the timestamp representation, added a USD amount
   using the simulator's fixed exchange rates, and derived `transaction_date`.
   For `evaluation_123_0000000`, GBP 12.65 becomes USD 15.8125. These are simulation
   rates, not current exchange rates.
3. Read [data_quality_results.csv](data_quality_results.csv). These are all 12
   checks for the **full 100-row evaluation split**, each with zero failures.
   They cover schema, required values, types, duplicates, amounts, timestamps,
   categories and country/currency consistency. Statistical anomalies can still
   be valid records. No quarantine rows are shown because this run had none.
4. Join [anomaly_predictions.csv](anomaly_predictions.csv) by `transaction_id`.
   `1` means anomaly and `0` means normal. `actual_anomaly` is the separate
   evaluation truth copied from the saved prediction output, not a model input.
   Positive Isolation Forest scores are flagged; scores are not probabilities.

| Transaction ID | Actual | Rules | Isolation Forest | Combined OR | What it illustrates |
|---|---:|---:|---:|---:|---|
| evaluation_123_0000000 | 0 | 0 | 0 | 0 | Normal record correctly left unflagged |
| evaluation_123_0000005 | 1 | 1 | 0 | 1 | Isolation Forest false negative caught by rules |
| evaluation_123_0000006 | 0 | 0 | 1 | 1 | Isolation Forest false positive |
| evaluation_123_0000010 | 1 | 1 | 1 | 1 | Both detectors identify an actual anomaly |

## Read the full evaluation

[evaluation.json](evaluation.json) preserves the actual metrics and confusion
matrices for **all 100 evaluation records**, not just these 10 examples.

| Detector | Precision | Recall | F1 |
|---|---:|---:|---:|
| Rules | 1.0000 | 1.0000 | 1.0000 |
| Isolation Forest | 0.2500 | 0.2000 | 0.2222 |
| Combined OR | 0.6250 | 1.0000 | 0.7692 |

The Isolation Forest confusion matrix is `[[92, 3], [4, 1]]`, with actual classes
as rows and predicted classes as columns. The rules baseline performs better on
this small synthetic split. These results do not establish production fraud
performance. Do not recompute full-dataset metrics from the selected 10 rows.

[validation_summary.json](validation_summary.json) separates the verified Spark
counts from the local checks used to prepare these exports. The input, clean and
prediction subsets each contain 10 rows; quality results cover all 100 rows.
CSV outputs are column projections for readability. No values or model scores
were adjusted, and JSON metrics were only pretty-printed.

## Trace the evidence

[provenance.json](provenance.json) records the six source object URIs, immutable
GCS generations, complete-source SHA-256 hashes, selected transaction IDs,
exported columns and public-file checksums. Source hashes and sample hashes
are different because the public exports are smaller or reformatted. GCS
references identify private source objects; they are not public download links.

The clean and prediction artifacts were reconciled with BigQuery during the
verified publication-only recovery. These examples were exported from GCS;
they are not a new BigQuery query result. See the [verified cloud results](../docs/cloud-pilot-results.md)
for the six published table counts, job IDs and recovery boundary.

Previous Cloud Workflows executions and Vertex CustomJobs remain **FAILED**.
Spark processing and saved ML artifacts succeeded, and BigQuery publication
completed through a separately verified recovery path. The examples do not
claim a successful full Workflows execution.

All records and customer identifiers are synthetic. No credentials, private
logs, full datasets or model binaries are included.
