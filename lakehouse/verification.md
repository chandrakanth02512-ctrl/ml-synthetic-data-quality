# Offline verification and managed-execution limits

Verified in Codex Cloud on 31 October 2026, Python 3.12.14, Java 21,
PySpark 4.0.1. Main and PRs #2/#3 are preserved. No live GCP operation,
resource/IAM/protection change, build, batch or workload execution was performed.

| Check | Observed result |
|---|---|
| Feature suite, actual local Spark + offline SDK boundaries | **61 passed** |
| Original application regression suite | **164 passed; 95.88% coverage** |
| Existing application files | **46 original Git blob hashes unchanged** |
| Spark pilot fixture | **256 source, 252 clean, 4 quarantined, 12 quality rows, 256 ledger rows** |
| Reconciliation SQL relational logic | Executed in DuckDB after GoogleSQL dialect translation: missing/extra/duplicate rows, changed payload hash, amount and disposition detected |
| GoogleSQL syntax checks | Explicit schemas/DDL and publication/reconciliation statements parsed with sqlglot's BigQuery dialect; this is not BigQuery service validation |
| Dependency consistency | All **48** installed packages compatible in the isolated feature environment |
| Ruff lint/format | Passed for new package, tests, tools and driver |
| Packaging | Deterministic code-only ZIP imports and hashes successfully without site packages; existing destinations refused |
| Credential/private-file checks | Only allowlisted new source/tests/SQL/schema/docs/CI files selected; no credentials, receipts, generated datasets, models or archives committed |

Tests include reuse of original generator/schema/validator, null/type/schema errors,
duplicate groups, invalid UTC/calendar/amount/Unicode whitespace cases, malformed
JSON, complete quarantine, source generation/hash/size limits, partial uploads,
lost acknowledgements, immutable replay/conflicts/corruption, table drift, regional
mismatch, fixed-ID load/publication replay, failed job preservation, ambiguous load
reconciliation, job configuration collision, API schema aliases and SQL injection
rejection. Every live client constructor in feature tests is replaced by a guard.
Only Spark's local loopback sockets are used; no GCP identity is required.

Two environments intentionally remain separate. Original Cloud Build/container/CI
constraints are unchanged. The feature requirements pin GCS 2.19 and BigQuery 3.38
to the documented Dataproc 3.0 SDK versions, and dateutil 2.9.0.post0 for compatibility.
Applying original dateutil 3.0 constraints to this feature would create a dependency
conflict. The original application suite also passed in the compatible feature
environment; current-head GitHub CI must independently validate both environments.

The real Spark tests are hosted/local execution, **not a managed Dataproc batch**.
GCS/object and BigQuery/job API boundaries use fakes. BigQuery publication's actual
transaction/ASSERT/conditional semantics, IAM, service-agent setup, runtime package
availability, network connectivity, Mumbai quotas, regional capacity and actual costs
remain subject to separately approved managed acceptance. SQL parsing and a local
relational engine cannot establish managed GoogleSQL execution success.

No CI job has OIDC permission, GCP credentials, gcloud submission, deployment or
workload execution. All proposed managed commands in the runbook require separate
owner authorization. The phase's future live test is a new Dataproc pilot, not a
repeat of the successful Cloud Build, archive verification or existing Run smoke.
