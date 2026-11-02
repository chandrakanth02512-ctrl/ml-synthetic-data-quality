# Verified cloud pilot results

Verified on 2 November 2026 in `gcp-intelligent-data-quality`, `asia-south1`.
All data is synthetic. The preserved source run is `workflow-smoke-002`.

Both Spark batches succeeded. Vertex created the saved Isolation Forest model,
verified its reload, and generated 100 predictions. Its CustomJob then failed
while publishing to BigQuery: the previous 100,000,000-byte script limit was too
small for the script's aggregate billed statements. The original Workflows
execution and Vertex CustomJob remain FAILED.

A separate publication-only recovery reused those immutable artifacts. It did
not run Spark, retrain the model, submit a Vertex job, execute Workflows, upload
GCS objects, or change IAM or infrastructure. The three publication scripts
committed successfully with a 512 MiB limit each.

## Published data

Dataset: `gcp-intelligent-data-quality.synthetic_quality_lakehouse`.

| Table | Verified rows |
|---|---:|
| clean_transactions | 356 |
| quarantined_records | 0 |
| quality_results | 24 |
| pipeline_runs | 2 |
| anomaly_predictions | 100 |
| model_runs | 1 |

An independent read-back compared clean, quarantine, quality and prediction rows
with their immutable GCS sources, checked both pipeline receipts and the model
receipt, and found no duplicate keys. All three scripts and their child jobs
finished without errors, including COMMIT_TRANSACTION. All 58 expected GCS
references matched their generations and SHA-256 hashes, including the 52
preserved run-001/run-002 evidence objects. Failed jobs and reservations were
not replaced or deleted.

## Actual model results

The evaluation split contains 100 transactions and five anomaly labels. Labels
were separate from training inputs. These are the preserved model's measured
results, not targets or newly fitted results.

| Detector | Precision | Recall | F1 |
|---|---:|---:|---:|
| Rules | 1.0000 | 1.0000 | 1.0000 |
| Isolation Forest | 0.2500 | 0.2000 | 0.2222 |
| Rules OR Isolation Forest | 0.6250 | 1.0000 | 0.7692 |

The rules baseline outperformed this Isolation Forest on this small synthetic
split. These results do not establish production fraud-detection performance.

Model SHA-256:
`a2c6e36c45a520051c4ea154aa560e862dcd5630b94979f12655998184a4a482`.

## Recovery evidence and safeguards

Attempt: `publication-recovery-002-002`.
Manifest SHA-256:
`8b6621e93b032b1ed4d789ea179634df2bdecc501b53655cb29b6c7c74f9e1b3`.

BigQuery job IDs in `asia-south1`:

- `pubrec_f36c471be4a8adff11673d0ba04f6e94_anchor`
- `pubrec_f36c471be4a8adff11673d0ba04f6e94_training`
- `pubrec_f36c471be4a8adff11673d0ba04f6e94_evaluation`
- `pubrec_f36c471be4a8adff11673d0ba04f6e94_predictions`

The anchor binds the recovery configuration to an immutable attempt. Existing
successful load jobs are reused only when their configurations match. Target
writes and receipts share transaction boundaries; failed jobs are not replaced.
Lost acknowledgements are reconciled through deterministic job IDs. Atomicity
is per publication script, not across all three scripts.

The first local recovery stopped on a GCS download timeout before submitting any
BigQuery job. Recovery reads now use explicit 10-second connection and 30-second
read timeouts, with at most two attempts for transient errors. Permission,
generation and checksum failures are not retried. The historical error did not
record its object URI; later diagnostics captured intermittent reads on Spark
artifacts. The underlying transport cause has not been established.

The successful query jobs report 471,859,200 billed bytes (450 MiB) combined;
the anchor reports zero billed bytes. Actual invoiced cost and free-tier
application are not available from job statistics. No additional Dataproc or
Vertex compute was started for recovery.

The selected recovery/Workflows/Vertex/loader suite initially passed 233 offline tests,
including real local SQL transaction rollback, reconciliation, duplicate
prevention, lost acknowledgements, conflicting attempts, bootstrap serialization
and bounded-read regressions. The original core suite passed 164 tests. Private
cloud reports, downloaded data and credentials are excluded from this repository;
only the reviewed result summary and minimal regression fixtures are included.

The 900-second local wrapper expired during its repeated final GCS verification,
after all publication scripts committed. It did not write its final CLI report.
Independent verification and post-deadline inspection confirmed the exact counts,
matching sources/receipts, successful child transactions and no active BigQuery
jobs. The wrapper timeout was not a BigQuery failure or cancellation. No second
publication was submitted. A separate read-only check reconfirmed the counts
before GitHub publication.

The final source review removes the operator-specific default account: the CLI
now requires an explicit `--account`. This does not alter any cloud artifact or
published row. The final offline suites passed 443 tests: 164 core, 65 lakehouse,
62 Vertex and 152 Workflows/recovery tests. The earlier selected suite overlaps
these counts and is not added to that total.

A successful full managed Workflows execution is still unverified. Future
execution needs a separately reviewed immutable plan/package identity; the old
failed run must not be blindly resumed or resubmitted.
