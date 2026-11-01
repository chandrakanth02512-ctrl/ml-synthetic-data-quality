# Workflows: one bounded end-to-end pilot

Stacked on **PR #5, `bbc2e698ca5bf176aa28234d13e77108993cc048`**, which depends
on PR #4. All 102 base files, historical image/build evidence and recovery receipts
are preserved. No workflow, image build, IAM change or managed execution has run.

`pipeline.yaml` is a real Google Cloud Workflows definition, **disabled by default**.
The offline renderer embeds one fixed reviewed pilot. Execution arguments cannot
supply another image, job body, input, SQL statement, identity or resource name.
A future operator must also send `{"confirm":"RUN workflow-smoke-001","mode":"start"}`.
There is no schedule, automatic trigger, endpoint or GPU.

## Flow and reused interfaces

1. Check exact GCS generations/sizes and hash the small standalone bootstrap.
   Dataproc bootstrap and existing ingestion verify package, configuration and source
   SHA-256 before importing/processing. Large objects never enter Workflows memory.
2. Sequential **training/evaluation Dataproc batches** call unchanged
   `synthetic_quality_lakehouse.driver.run`. Each produces clean, quarantine, quality
   and ledger artifacts plus completion. The wrapper enforces expected clean counts
   and a configurable quarantine allowance before the batch can succeed.
3. One **Vertex CustomJob** calls unchanged `synthetic_quality_vertex.train.run`.
   It binds exact Dataproc completion generations/hashes, rechecks quality, fits the
   original unsupervised model and training-only rules, then evaluates independent
   truth for rule/Isolation Forest/hybrid. Existing split/leakage checks remain active.
4. The same CPU worker verifies all model/prediction/report artifacts, then calls
   unchanged lakehouse and prediction **BigQuery loaders**. Their fixed job IDs,
   WRITE_EMPTY stages, permanent receipts, transactions and reconciliation SQL are
   retained. Polling/API calls have a shared 240-second publication deadline and
   no job resubmission retry. Final tables/dataset must already be approved/created.
5. Worker completion is written last with exact source/model/report references and
   all three publication receipts. Workflows requires success, expected row counts,
   complete report contract, reconciliation and current generations for all 17
   verified objects before its durable completion checkpoint and structured log.

Source: `src/synthetic_quality_workflows/{plan,bootstrap,runner}.py`.
No existing processing/model/SQL implementation was copied or changed. BigQuery
publication happens inside the Vertex worker; no separate HTTP service is added.

## Duplicate prevention and recovery

Every workload has a **create-only GCS attempt record before one POST**. Only the
execution receiving the successful initial write owns submission. A 412, lost
acknowledgement or interrupted reservation never grants ownership. A concurrent
execution cannot submit behind that reservation.

Dataproc uses a deterministic batch ID and UUID requestId. Vertex does not provide
this create idempotency key: resume inventories every matching page (bounded to 10),
requires exactly one job and validates its request, identity, CPU, image/command,
arguments, retries and access settings. Labels alone are insufficient. Zero or
multiple matches stop for manual review. Read-only polling also revalidates the job.
Neither submission POST has a retry policy. Only GETs retry transient errors, at most
three times; polling has a bounded deadline. Failed/cancelled/unknown jobs stop.
An acknowledged submission that is briefly invisible also stops; no new POST follows.

A separately requested `mode: resume` rechecks the same resources and checkpoints.
A duplicate `start` stops. Resume can finish an interrupted Workflows execution
without new batches, CustomJobs or BigQuery publications. Failed Vertex jobs are
**not** automatically replaced even if a BigQuery response was ambiguous. The Python
worker replay tests prove fixed-ID loader recovery; they do not authorize a new job.
Preserve all attempts/receipts and inspect existing jobs before an owner-approved
recovery decision. Never delete a marker or change run IDs to bypass uncertainty.

Checkpoints live only below
`gs://synthetic-quality-984592102391-data-as1/lakehouse/orchestration/runs/RUN_ID/`.
Training artifacts have their own `lakehouse/vertex-workflows/runs/RUN_ID/` prefix.
Safe failure reports contain category, stage/state, HTTP code and execution/run IDs;
raw HTTP/policy contents and credentials are not logged. Use `--call-log-level=none`
for a future deployment so HTTP calls and embedded bootstrap are not automatically
logged. Logs are not checkpoints. Failure-report write errors preserve the primary
failure. No cleanup/deletion, automatic API setup or old recovery-marker access exists.

## Offline validation

Use a cloud checkout, Python 3.12 and Java 21, with PR #4's isolated dependencies:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]' -r requirements-lakehouse.txt
.venv/bin/python -m pip check
SPARK_LOCAL_IP=127.0.0.1 .venv/bin/python -m pytest -c tests_workflows/pytest.ini tests_workflows --cov=synthetic_quality_workflows
SPARK_LOCAL_IP=127.0.0.1 .venv/bin/python -m pytest -c tests_vertex/pytest.ini tests_vertex -q
SPARK_LOCAL_IP=127.0.0.1 .venv/bin/python -m pytest -c tests_lakehouse/pytest.ini tests_lakehouse -q
.venv/bin/python -m pytest tests --cov=synthetic_quality --cov-fail-under=94.5
.venv/bin/python -m ruff check src tests tests_vertex tests_lakehouse tests_workflows tools jobs
.venv/bin/python -m ruff format --check src tests tests_vertex tests_lakehouse tests_workflows tools jobs
```

Codex Cloud results: **81 Workflows tests passed; 91.56% statement-and-branch
coverage** (217/230 statements, 65/78 branches). All **62 Vertex**, **65 Dataproc/
BigQuery** and **164 original application** tests passed; application coverage remains
**95.88%**. Ruff, actionlint, dependency consistency, runtime-overlay resolution,
wheel/sdist contents, deterministic package imports, credential-signature scanning
and all 102 base-blob preservation checks passed. No new test dependency was added.

Tests interpret the **actual YAML** through a restricted, fail-on-unknown offline
interpreter, with injected REST effects, documented expression helpers and loop scope.
One integration actually runs local Spark → quarantine/quality → sklearn evaluation
→ existing loaders against fake GCS/BigQuery. This is not a Google Workflows emulator
or managed API/compiler validation. Tests do not prove a live deployment will succeed.
Runtime image construction and real GCP acceptance remain unverified.

## Small pilot and remaining approved operations

The example deliberately contains invalid immutable references and stays disabled.
Bounds: at most 256 clean training / 100 evaluation rows, at most 10 quarantined
records per split (example 1), sequential Dataproc 3.0 batches with driver + two
executors (**12 vCPUs each**, 600-second TTL), one n1-standard-4 CPU Vertex worker
(1,200-second timeout, retries/restart disabled), 100 MB maximum billed per publication
query. Default workflow polling deadline is 900 seconds per stage. Queue/provisioning
and real service behavior can exceed it: a timeout stops orchestration without
cancelling or resubmitting the underlying job. These are workload bounds, not a cost
cap; the earlier Cloud Run smoke allowance does not cover this different pilot.

**Separate owner approval is required before any numbered live action:**

1. Review/merge dependencies and this feature in order through normal PR approval;
   no merge is performed here. Confirm service availability, effective scoped
   permissions, policy/network access and quota headroom without repeating unrelated
   audits. Approve only missing APIs/agents, identities, regional dataset/tables and
   network configuration. Proposed Workflows identity:
   `synthetic-quality-workflows@gcp-intelligent-data-quality.iam.gserviceaccount.com`:
   GCS input/checkpoint get/create, logging, Dataproc create/get and Vertex create/get/list,
   plus actAs on the two runtime accounts. Spark keeps its existing dedicated account;
   Vertex additionally needs scoped data/artifact read/create, dataset publication and
   project jobs.create. No keys or blanket admin roles. Workflows has no BigQuery write
   role. Its runtime account cannot change IAM.
2. Build **one new CPU runtime image** using `workflows/Dockerfile` and approve its
   verified immutable digest. It inherits the existing verified CPU image, adding
   the BigQuery SDK (absent in the original image) and its required
   `python-dateutil<3` compatibility pin. Every other existing runtime pin is retained. No model source rebuild is
   implied. No image digest is guessed or marked verified. Offline dependency
   resolution is not an image build or vulnerability attestation.
3. Generate/stage the existing Vertex 256/100 synthetic fixture and separate truth
   through reviewed create-only GCS paths. Fill `pilot.example.json` in `.tmp/` with
   exact generations/hashes, image digest, reviewed subnet and feature revision.
   The tool replaces package/bootstrap placeholder refs. Approve staging separately:

```bash
.venv/bin/python tools/workflow_assets.py stage .tmp/pilot.json --approve-staging > .tmp/workflow-bundle.json
.venv/bin/python tools/workflow_assets.py render .tmp/workflow-bundle.json --output .tmp/pipeline.yaml
```

   Staging checks clean source/revision and all input hashes before writing. Outputs
   are content-addressed/create-only; packaging/rendering refuse existing local paths.
   Keep `enabled: false` until owner approval explicitly covers one end-to-end pilot,
   its BigQuery publication, quotas and costs. An enabled plan changes its hash;
   restage/render/review it as a new immutable approval artifact before deployment.
4. After separate configuration/deployment approval, deploy the reviewed rendered
   definition (not the disabled template) as `synthetic-quality-pilot` in Mumbai with
   the dedicated Workflows account, no schedule and `--call-log-level=none`.
5. After separate **execution** approval, dispatch exactly once with the action-specific
   confirmation and `mode: start`. Record the Workflows execution ID. Poll/read the
   result. Require two successful batches, one successful CustomJob, matching model
   artifacts/three-detector metrics, three successful publication receipts, SQL
   reconciliation and the workflow completion record. Inspect actual cost/usage.
6. For an interrupted execution, inspect the recorded resources first, then approve
   `mode: resume` for the same deployed plan. Ambiguous/failed resources stop for
   manual review. Cleanup/cancellation requires another scoped owner decision.

Documented APIs/syntax checked against official references:
[HTTP OAuth2](https://cloud.google.com/workflows/docs/http-requests),
[GET retry policy](https://cloud.google.com/workflows/docs/reference/syntax/retrying),
[loop scope](https://cloud.google.com/workflows/docs/reference/syntax/iteration),
[Dataproc batchId/requestId](https://cloud.google.com/dataproc/docs/reference/rest/v1/projects.locations.batches/create),
[Vertex CustomJob create](https://cloud.google.com/vertex-ai/docs/reference/rest/v1/projects.locations.customJobs/create),
[GCS create precondition](https://cloud.google.com/storage/docs/json_api/v1/objects/insert).
No Workflows connector silently retries workload creation: the definition uses raw
OAuth2 REST calls and explicit GET-only retry blocks.
