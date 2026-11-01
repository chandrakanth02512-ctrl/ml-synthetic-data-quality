# Vertex implementation verification

Verified in Codex Cloud on 1 November 2026, Python 3.12.14 / Java 21. No live Vertex,
BigQuery, GCS, IAM, API enablement, endpoint, deployment or paid workload operation
was performed. This feature is stacked on PR #4 at `05bc60b606bfa0ca1ac465d64587be665e8f3212`.

| Check | Result |
|---|---|
| Vertex offline suite | **62 passed** |
| Vertex statements + branches | **93.40%** (419/439 statements, 105/122 branches) |
| Existing Dataproc/BigQuery suite | **65 passed** |
| Original application suite | **164 passed; 95.88% application coverage** |
| Original application / PR #4 source preservation | All **79 base blobs** unchanged |
| Dependencies | Existing isolated feature environment reused; all 60 installed packages compatible; no new dependency required |
| Ruff lint/format and actionlint | Passed |
| Packaging | Deterministic code-only ZIP imports, preserves original model bytes and refuses overwrite |

Tests perform actual sklearn model/rule fitting, label-aligned evaluation and trusted
model reload. One integration runs actual local Spark on independent 256/100-row
splits, quarantines an additional invalid training row, consumes the true Dataproc
completion artifacts, trains/evaluates and passes predictions to a fake BigQuery API.
Other tests cover split overlap/time-order/label leakage, strict evaluation truth,
all three detector metrics, deterministic models/reports, immutable replay, partial
failure/lost acknowledgements, corrupted inputs/artifacts, schema/region drift,
fixed-ID publication recovery, staged bytes, exclusive CustomJob reservations,
ambiguous/failed jobs and no automatic resubmission. Procedural publication SQL is
parsed with SQLFluff; malformed statements are rejected. No external service is
substituted for local Spark/sklearn computation.

All cloud clients/HTTP/credential discovery are guarded or explicitly injected with
fakes. There are no test credentials. Coverage data for the Vertex and original
suites was isolated to avoid concurrent coverage-file interference. CLI module-launch
guards and a few error branches remain uncovered; test coverage is not managed
execution proof.

The job specification reuses the owner-verified immutable CPU image and overrides
its old entrypoint with a hash-checked GCS code/config bootstrap. No new image build
was needed or submitted. The image has different SDK/dateutil versions from the
operator test environment; actual runtime versions are recorded, and equality across
different environments is not claimed. Original image/source verification was not
repeated. Historical build/recovery receipts are never read or changed by this code.

Remaining managed checks require separate approval: API/service-agent and dedicated
identity setup, image pulls, Mumbai Vertex availability/CPU quota, policy/network
compatibility, exact staged references, one CustomJob, completion artifacts, and
prediction-table publication. A Vertex SUCCEEDED state alone does not prove artifact
verification or BigQuery publication. No endpoint, GPU, model registry deployment,
hyperparameter tuning service or unrestricted administrative access is introduced.
