# GCP Stage 2 recovery from GitHub

Recovery tools live in the existing repository on branch **`deploy/stage2-recovery`**.
No ZIP transfer or Python package installation is needed in Google Cloud Shell.
This branch and its draft PR do not approve a build, job deployment or execution.

The deployment-tool checkout is separate from the application build checkout.
Application source remains pinned to:

```text
34cc3d46b662a9f0560c3ff846d68b327d52e4b8
```

The original application files, tests, dependencies, Dockerfile, Cloud Build YAML,
upload allowlists, documentation and main CI are preserved byte-for-byte. The
deployment additions are excluded by the existing container/source allowlists.
`application-baseline.json` records all 46 original Git blob hashes; offline CI checks
them. Remote `main` must still equal the application pin before recovery proceeds.

## Retrieve the deployment branch in Cloud Shell

Run these commands as the same signed-in Cloud Shell user who owns the original
build-attempt marker. A fresh directory preserves all existing checkouts and receipts:

```bash
git clone --branch deploy/stage2-recovery --single-branch \
  https://github.com/chandrakanth02512-ctrl/ml-synthetic-data-quality.git \
  "$HOME/ml-synthetic-data-quality-deployment"
cd "$HOME/ml-synthetic-data-quality-deployment"
git branch --show-current
git rev-parse HEAD
```

Git clone refuses a nonempty destination. If it exists, stop and inspect it; do not
reset, clean, overwrite or delete it. The expected branch is `deploy/stage2-recovery`.
Record the commit and compare it with the reviewed draft PR before live checks.
No service-account key, downloaded credential, Windows command or laptop execution
is required. The recovery tools use Python 3.10+ standard library, Git and `gcloud`.

## One read-only recovery preflight

From the repository root:

```bash
python3 deploy/gcp/stage2.py recovery-check
```

This command performs reads and permission tests only. It never enables APIs,
changes IAM, creates infrastructure, submits builds, creates/executes jobs, deletes
resources or writes attempt receipts. A successful result is not deployment approval.

It requires and rechecks:

- ACTIVE project `gcp-intelligent-data-quality`, region `asia-south1`, billing link,
  required APIs and effective organization policies; the reviewed GitHub main SHA.
- Both planned buckets, private Docker repository, custom role, all four Stage 1
  workload IAM bindings and the Build/Run service-agent roles.
- Operator effective `iam.serviceAccounts.actAs` on both dedicated accounts,
  build/job project permissions and source-bucket object upload/read permissions.
- Exact original build **`6f75c5dd-cc3a-4d47-9c9d-43f51fa3c4ba`**, with both Build and
  Docker step `CANCELLED`, expected identity/source/settings and no published image.
- Complete dedicated image inventory (including untagged images) empty; no Cloud Run
  Jobs in Mumbai; no additional application build or reserved recovery attempt.
- Existing original attempt marker, matching project/revision/action, fingerprinted
  without modification. Marker location is independent of the Git checkout:

```text
~/.local/state/synthetic-quality-phase1/gcp-intelligent-data-quality-build-34cc3d46b662a9f0560c3ff846d68b327d52e4b8.attempt
```

Expected final output: `RECOVERY PREFLIGHT PASSED (read-only). No new approval is implied.`
Stop on any failure. Permission errors are never treated as resource absence.
Locally unset policies still require inherited/default evaluation. Unsupported
constraints are reported distinctly. APIs are not enabled automatically. Quotas,
conditional/inherited IAM, deny/PAB/VPC restrictions, billing standing, budget alerts,
service capacity and actual cost still require review; live success is not guaranteed.

## Preserved resources and receipts

The existing `gcp-idq-data-984592102391` bucket is protected and excluded from reuse,
uploads, IAM changes, configuration changes and cleanup. Its matching Mumbai,
Standard, uniform-access, public-access-prevention and seven-day soft-delete settings
do not authorize adoption. `INVENTORY.json` records user-supplied state, not a fresh
GCP query by Codex.

The cancelled build is historical evidence, never a successful image candidate.
Its record and original attempt receipt remain intact. No marker deletion, rename,
overwrite, reset or recreation is part of recovery.

## Future operations: separate explicit approvals required

Do not run these as part of GitHub preparation or read-only preflight.

| Command from repository root | Scope and gate |
|---|---|
| `python3 deploy/gcp/stage2.py recovery-submit` | One additional paid build, only after new explicit approval and exact interactive acknowledgement. |
| `python3 deploy/gcp/stage2.py recovery-status` | Read-only observation after submission/interruption; no retries or receipt writes. |
| `python3 deploy/gcp/stage2.py recovery-deploy` | Separate approval to create the job by verified image digest; no execution. |

The approved submission path clones the application into a fresh isolated directory,
checks out the full reviewed application SHA **detached**, verifies the clean tree
and remote main pin, then shows the source upload allowlist. It builds only from that
application checkout, never from this deployment branch. The GitHub `cloudbuild.yaml`
is unchanged. The worker is `e2-standard-2`, timeout 1200 seconds, dedicated build
account, and existing `CLOUD_LOGGING_ONLY` configuration.

A distinct replacement tag identifies the attempt without changing application code:

```text
34cc3d46b662a9f0560c3ff846d68b327d52e4b8-recovery1
```

An exclusive recovery marker is durably written before the single asynchronous
submission. It binds the cancelled ID, original receipt hash, project/region,
application SHA, replacement tag, operator and time. Separate write-once JSON
receipts preserve acknowledgement/evidence. The original marker is checked before
and after submission and is never written by this path.

If submission times out, disconnects, fails, or loses its acknowledgement, the new
marker remains consumed. Use `recovery-status`; do not resubmit or remove receipts.
No failed/cancelled replacement authorizes a third attempt. Exactly one server-side
replacement can be reconciled after a lost acknowledgement using the bound tag,
identity/settings, staging prefix and creation time. Clock skew can cause a
conservative stop; review evidence rather than editing receipts.

Deployment accepts only the exact known cancelled record plus one successful
replacement. It compares the Build-result digest with full registry inventory and
the replacement tag before create-only job deployment. It never selects "latest
successful", treats cancellation as success, or overwrites an existing job.
The job remains 1 task, parallelism 1, 1 vCPU, 1 GiB, 600 seconds, zero retries,
256/100 rows, seeds 42/123. There is no execute-now flag. Stage 3 execution remains
separately gated and is not included in this change or recovery approval.

## Offline verification and dependencies

Run in Codex Cloud or CI; no GCP authentication is needed:

```bash
python3 -m unittest discover -s deploy/gcp -p 'test_*.py' -v
```

All cloud/Git/process/network calls in deployment tests are mocked or blocked, and
receipt fixtures use temporary HOME directories. Integration checks verify original
Git blob hashes, standard-library-only imports, private-artifact exclusion and the
detached application pin. Existing ML tests and dependency checks remain in the
original credential-free CI workflow. A separate read-only GitHub Actions workflow
runs deployment tests; it has no secrets, OIDC, gcloud or deployment actions.

No credentials, tokens, keys, local virtual environments, real receipts, generated
models/data, ZIPs or prior private files are included. Public project/bucket IDs and
the cancelled build ID are intentional deployment configuration. See
`verification.md` for observed results and limitations.

The cancelled attempt may already have incurred charges. One replacement adds build
compute and retained image/source/log storage. The earlier ₹100 allowance must be
reviewed against actual spend; it is not a cap or guarantee. The ₹500 budget only
alerts. Use one operator/home and invocation at a time: local receipts and server
inventory cannot guarantee distributed at-most-once behavior against concurrent
operators or deleted/copied history. Preserve all existing resources and evidence.
