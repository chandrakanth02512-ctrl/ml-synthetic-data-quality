# Vertex AI CPU training and evaluation

This is a **stacked feature based on draft PR #4 at
`05bc60b606bfa0ca1ac465d64587be665e8f3212`**. Its training path depends on PR #4's
Dataproc completion/clean-data contract and GCS adapter; prediction publication
reuses its BigQuery `job_once` recovery. All PR #4 and original application files
are preserved. Review this PR relative to `feat/dataproc-bigquery`, not main.
After an approved PR #4 merge, retarget/rebase through a separate reviewed action.
Nothing here authorizes a merge or managed operation.

## Implemented flow

1. Read separate Dataproc training/evaluation completion manifests by exact GCS
   generation and SHA-256. Validate their completion inventory/counts, immutable
   clean-object hashes and row lineage. Recheck the seven-field transaction contract.
2. Reject overlapping transaction IDs, identical source inputs, and overlapping
   time periods. Training must precede evaluation. Sort both splits by ID.
3. Fit the **existing** training-only preprocessor + Isolation Forest and calibrate
   the **existing** rule baseline on training data only. No labels enter fitting,
   preprocessing, thresholds or hyperparameter selection. No evaluation refitting.
4. Calculate rule, Isolation Forest and hybrid OR predictions. Only then read a
   separate evaluation truth object and align its labels by transaction ID.
5. Save precision, recall, F1, fixed 2×2 confusion matrices and counts for all three
   approaches, in reproducible JSON/CSV. Persist the trusted joblib model, rule
   baseline, runtime/input/code metadata and BigQuery-compatible predictions.
6. Publish create-only GCS artifacts and completion last. Recover matching partial
   writes; completed replay verifies objects without fitting again.
7. Separately approved prediction publication loads an immutable stage with fixed
   BigQuery job IDs, then inserts predictions and a permanent model-run receipt in
   one transaction. SQL verifies counts, row identity, binary/hybrid outputs, model
   hash and exact source-to-target equality. No target overwrite or automatic retry.

Only clean transactions are evaluated. Quarantined counts are retained in model
metadata; truth IDs must match the clean evaluation set **exactly**. Extra, missing,
duplicate or nonbinary truth labels stop rather than silently change the denominator.
Anomaly labels are simulator truth, not independently reviewed fraud truth. Existing
model limitations remain; this stage does not claim Isolation Forest beats rules.

Boundaries: 10,000 rows per split; 10 MiB manifest/truth inputs; one CPU worker,
`n1-standard-4`, 600-second scheduling timeout, no accelerators/endpoints, at most
200 trees/256 samples, fixed seed. Original model/feature/evaluation code is unchanged.
Reproducibility is within the recorded code/input/runtime versions; cross-version
binary/model equality is not claimed. A different run ID is a new experiment.

## Reuse of the verified image — no new build

The CustomJob uses the existing immutable CPU image:

`asia-south1-docker.pkg.dev/gcp-intelligent-data-quality/synthetic-quality-phase1/synthetic-quality@sha256:35a1e032472e06e4b21c55996b46faa0090deef3a4bdee710f27ddcb85e33116`

The old image does **not** already contain the new Vertex code. `containerSpec.command`
overrides its Cloud Run entrypoint with the checked-in bootstrap. That command reads
an exact-generation, hash-checked code ZIP and configuration from GCS, adds the ZIP
to Python's import path and runs `synthetic_quality_vertex.train`. It performs no
pip installation or archive extraction. The ZIP includes the original model code,
PR #4 adapters and this feature. Generated package/config references are reviewed
before submission. No tag is used as source proof. No new Cloud Build is needed.

The image has Python 3.12 and the previously verified sklearn/numpy/pandas packages.
The operator/test environment uses PR #4's separate compatible BigQuery environment.
Actual package versions are recorded in each training request; changing them cannot
silently reuse an existing run. Only a just-created local trusted model is reloaded;
neither the bootstrap nor BigQuery publisher unpickles an arbitrary remote model.

## Offline checks — Codex Cloud / credential-free CI

From a fresh checkout with Python 3.12 and Java 21:

```bash
test ! -e .venv
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]' -r requirements-lakehouse.txt
.venv/bin/python -m pip check
SPARK_LOCAL_IP=127.0.0.1 .venv/bin/python -m pytest -c tests_vertex/pytest.ini tests_vertex \
  --cov=synthetic_quality_vertex --cov-report=term-missing
SPARK_LOCAL_IP=127.0.0.1 .venv/bin/python -m pytest -c tests_lakehouse/pytest.ini tests_lakehouse -q
.venv/bin/python -m pytest tests --cov=synthetic_quality --cov-fail-under=94.5
.venv/bin/python -m ruff check src tests tests_lakehouse tests_vertex tools jobs
.venv/bin/python -m ruff format --check src tests tests_lakehouse tests_vertex tools jobs
```

Tests run real sklearn fitting/evaluation and one real local Spark → validated-data
→ training → fake BigQuery-boundary integration. Cloud SDK clients/HTTP/credentials
are blocked or replaced with fakes. Procedural prediction SQL is parsed using the
strict BigQuery dialect parser. These checks are **not** managed Vertex/BigQuery
execution. The new GitHub workflow has no Google auth, OIDC or deployment permission.
Original CI independently verifies the unchanged application dependency snapshot.

Local preparation, no cloud operations:

```bash
mkdir -p .tmp/vertex
.venv/bin/python tools/vertex_assets.py fixtures .tmp/vertex/inputs
.venv/bin/python tools/vertex_assets.py package .tmp/vertex/training.zip
.venv/bin/python -m synthetic_quality_vertex.predictions ddl \
  --project=gcp-intelligent-data-quality > .tmp/vertex/prediction-tables.sql
```

The fixture reuses the original generator: 256 normal training rows (seed 42),
100 independent February evaluation rows (seed 123), five injected anomalies and
a **separate** truth file. No labels appear in either transaction source file.
Existing destinations are refused. Code packaging is deterministic and includes
only Python/SQL files, never credentials, data, models or historical receipts.

## Future managed procedure — review/approval required

No live step below was performed. Do not run it merely because tests pass.
Project/region remain `gcp-intelligent-data-quality` / `asia-south1`. Mumbai Vertex
availability, CPU quota, service-agent image access, effective runtime permissions,
network/policy compatibility and cost remain unverified. The original Run image
verification is retained; managed Vertex execution is not established by it.

Proposed dedicated runtime identity:
`synthetic-quality-vertex@gcp-intelligent-data-quality.iam.gserviceaccount.com`.
It needs approved data/code/truth reads and create/read access on the new Vertex
output namespace. The Vertex service agent needs the approved image-pull access;
the operator needs CustomJob create/get/list and actAs on the runtime account.
Required Vertex API/service-agent/IAM setup and new prediction tables
require separate authorization. No keys, endpoints, GPUs, broad administrator grants
or modifications to existing Run/build accounts are included.

1. After separate approval, use PR #4's existing ingestion/batch process for **both**
   transaction files. Require two successful Dataproc completions, one per split.
   Record exact completion URI/generation/SHA-256 references. Do not feed raw
   transaction files or an unverified BigQuery export directly to this trainer.
2. After separate staging approval, stage evaluation-only truth:

```bash
.venv/bin/python tools/vertex_assets.py stage-truth .tmp/vertex/inputs/truth.ndjson \
  --run-id=vertex-smoke-001 --approve-staging
```

3. Fill `training.example.json` with those three real references, preserving model
   settings and the separate `lakehouse/vertex` output namespace. Placeholder values
   deliberately fail validation. Stage the reviewed clean feature checkout:

```bash
.venv/bin/python tools/vertex_assets.py stage-bundle .tmp/vertex/training.json \
  --approve-staging > .tmp/vertex/plan.json
.venv/bin/python -m synthetic_quality_vertex.custom_job plan --plan .tmp/vertex/plan.json
```

`plan` is offline. Staging is create-only, records generations/hashes and does not
submit a job. Keep reviewed package/config records; never delete/reset a marker to
force progress. The unrelated protected bucket is rejected by the inherited GCS
validator. Original recovery receipts and build-source objects are never accessed.

4. Only after explicit approval for one paid CustomJob:

```bash
.venv/bin/python -m synthetic_quality_vertex.custom_job submit \
  --plan .tmp/vertex/plan.json --approve-submission
```

This immediately starts managed training. It first verifies staged bytes, then
exclusively creates `submission.attempt.json` before exactly one CustomJob POST.
No retries, fallback identity or ambiguous-outcome resubmission. The Vertex request
also disables job retries and restart-on-worker-restart. Submission says SUBMITTED,
not training successful. The server assigns the job ID; this API path does not
offer the loader's deterministic BigQuery job-ID mechanism.

5. Read-only status/reconciliation after submission or a lost response:

```bash
.venv/bin/python -m synthetic_quality_vertex.custom_job reconcile --plan .tmp/vertex/plan.json
```

An acknowledged receipt reads its exact job; otherwise list by request hash and
require exactly one job whose full planned worker/identity/container settings match.
Zero/multiple matches, failed/cancelled jobs or mismatches stop. The existing marker
remains consumed. No automatic retry or new job ID is authorized by failure. One
operator is required; out-of-band writers/deleted history are outside this protocol.

6. Require both CustomJob SUCCEEDED **and** a valid Vertex `completion.json`, with
   256/100 rows, all artifact hashes/generations, saved-model reload verified and
   all three evaluation reports. Review actual metrics honestly. A completed GCS
   replay avoids fitting again, but submitting another managed job still costs money.
7. After separate table/publication approval, execute the reviewed printed DDL for
   `anomaly_predictions` and `model_runs` in the existing regional dataset. Existing
   tables must match the exact schema/ownership label. Then:

```bash
.venv/bin/python -m synthetic_quality_vertex.predictions load \
  --project=gcp-intelligent-data-quality --dataset=synthetic_quality_lakehouse \
  --manifest-uri=gs://synthetic-quality-984592102391-data-as1/lakehouse/vertex/runs/vertex-smoke-001/completion.json \
  --approve-publication
```

The loader reuses PR #4 `job_once`, sets a 100 MB per-query ceiling, creates only
an exact new prediction staging table, and publishes in one transaction. It never
overwrites a target. Check 100 prediction rows and one matching model receipt.
Failed/ambiguous BigQuery outcomes follow existing fixed-ID recovery, not automatic
replacement. No cleanup/deletion is automatic. Retained objects/stages and jobs may
incur charges; the earlier Run smoke allowance is not a guarantee for Vertex.

## Cloud Workflows contract for the next phase

The orchestrator must carry immutable completion/code/config references, invoke
two Dataproc validations, this one-time CustomJob submission protocol, poll the
returned CustomJob name, verify training completion/artifact hashes, then perform
separately authorized prediction publication. Keep run IDs/checkpoints, distinguish
SUBMITTED/RUNNING/COMPLETE/PUBLISHED, and route ambiguous create outcomes to read-only
reconciliation. Do not wrap CustomJob POST or BigQuery mutations in blind retries.
Managed acceptance of all services, scoped workflow identity/IAM and a small cost
review remain future explicitly approved work.

Official references: [CustomJob creation](https://docs.cloud.google.com/workflows/docs/reference/googleapis/aiplatform/v1/projects.locations.customJobs/create),
[container command overrides](https://docs.cloud.google.com/gemini-enterprise-agent-platform/machine-learning/training/configure-container-settings),
[CPU CustomJob configuration](https://docs.cloud.google.com/gemini-enterprise-agent-platform/machine-learning/training/create-custom-job).
