# GitHub Actions → GCP, without service-account keys

**Review package only. No live GCP/IAM changes, workflow dispatch, deployment,
execution or Cloud Build submission were performed.** The proposed authorization
must be reviewed and explicitly approved by the project owner before setup.

The application source pin remains
`34cc3d46b662a9f0560c3ff846d68b327d52e4b8`; the reported replacement image still
requires the live verification below. These workflows never build an image and
never access, delete or reset the Cloud Shell recovery receipts. PR #2 remains a
separate unmerged recovery-tools change; this authentication proposal does not
depend on its tools or run another recovery attempt.

## Current blocker: immutable image pin not supplied

The user reports a successful replacement image in live GCP. Its exact replacement
build ID and SHA-256 digest are not in GitHub, and Codex Cloud has no GCP identity,
`gcloud`, or permitted Google API egress. They have **not** been independently
verified. `config.json` therefore deliberately contains:

```json
{"enabled": false, "image_digest": null, "replacement_build_id": null}
```

No digest is guessed and no tag/latest fallback exists. Both manual workflows stop
before requesting GCP credentials while disabled; authorization `apply` also stops.
The rest of `config.json` fixes the planned data bucket and `smoke-001` namespace.
Both `environment_bypass_disabled_attested` entries also default to `false`.

Retrieve these values in an already authenticated Google Cloud Shell using reads:

```bash
gcloud builds list --project=gcp-intelligent-data-quality --region=asia-south1 \
  --filter='status=SUCCESS' \
  --format='table(id,status,createTime,results.images.name,results.images.digest)'

# Substitute the inspected successful replacement ID; do not select "latest" blindly.
gcloud builds describe REPLACEMENT_BUILD_ID \
  --project=gcp-intelligent-data-quality --region=asia-south1 --format=json

gcloud artifacts docker images list \
  asia-south1-docker.pkg.dev/gcp-intelligent-data-quality/synthetic-quality-phase1 \
  --project=gcp-intelligent-data-quality --include-tags --format=json

# Substitute the exact sha256:... digest returned by the successful Build result.
gcloud artifacts docker images describe \
  'asia-south1-docker.pkg.dev/gcp-intelligent-data-quality/synthetic-quality-phase1/synthetic-quality@sha256:REPLACE_WITH_64_HEX_DIGITS' \
  --project=gcp-intelligent-data-quality --format=json
```

Record the exact **replacement build ID**, **`sha256:` digest**, and **full immutable
image URI**. Exclude cancelled build `6f75c5dd-cc3a-4d47-9c9d-43f51fa3c4ba`.
Require Build and Docker-step `SUCCESS`, the declared replacement revision tag
`34cc3d46b662a9f0560c3ff846d68b327d52e4b8-recovery1`, and equality between the Build
published digest and Artifact Registry digest. Any differing provenance/settings
requires review, not weakened validation or another build.

The revision tag is a declared label, not independent proof of the source archive.
The owner must also inspect the Build's `source`/`sourceProvenance`, service account,
Docker step arguments and available provenance against the reviewed replacement
attempt. Do not activate if the source cannot be tied to the reviewed revision.

Have those public values recorded in `config.json` through code review. Enable the
configuration only as part of owner-approved activation; do not merely paste a tag.

## Trust and approval boundaries

| Check | Exact value |
|---|---|
| Project / number | `gcp-intelligent-data-quality` / `984592102391` |
| Region | `asia-south1` |
| Repository ID | `1436792250` |
| Repository owner ID | `341606319` |
| Git ref / ref type | `refs/heads/main` / `branch` |
| Event | `workflow_dispatch` |
| Deploy workflow/environment | `.github/workflows/gcp-deploy.yml@refs/heads/main` / `gcp-deploy` |
| Execute workflow/environment | `.github/workflows/gcp-execute.yml@refs/heads/main` / `gcp-execute` |

Both workflow code and Google provider CEL enforce the numeric repository/owner IDs,
main branch, manual event, exact workflow path and matching environment. Missing
claims fail closed. Trust does not rely on the repository name alone or assume the
older GitHub `sub` format. Two **separate workload identity pools** prevent one
provider's principalSet from impersonating both service accounts. Each provider
has one explicit audience, identical to its auth-action audience.

All actions are pinned to verified commit SHAs. Jobs request only contents/actions
read and OIDC-token write. A read-only pre-authentication guard verifies environment
reviewer rules and selected-branch policies. The auth action mints a masked,
300-second impersonated access token in memory: no credentials JSON input, generated
credential file, exported default credentials, permanent key or workspace artifact
upload is used. Tokens never enter command arguments, step summaries or receipts.

### Owner GitHub prerequisites — not configured by this PR

Before authorization/activation, configure **both** GitHub repository environments:

1. `gcp-deploy`: required reviewers for deployment; prevent self-review.
2. `gcp-execute`: required reviewers for execution; prevent self-review.
3. For each, use selected deployment branches with exactly one branch policy
   `main`, no tags/wildcards. "Protected branches only" is insufficient.
4. Disable administrator bypass in both environment settings. Only after inspecting
   the UI, explicitly attest each environment's disabled bypass in the reviewed
   `environment_bypass_disabled_attested` configuration map. Both entries must be
   `true` for activation. The guard rejects a reported bypass even with attestation;
   if the API omits that field it requires the owner attestation and reports that
   limitation. An unavailable setting blocks activation; do not attest to it.
5. Protect `main` and require review of workflow/configuration changes. Do not allow
   the trusted branch's code/configuration to change without the intended governance.

A different reviewer than the dispatcher is required. A solo owner cannot approve
their own dispatch under these defaults; select another eligible reviewer or bring
that policy choice back for explicit review. The package never silently weakens it.
Required-reviewer/environment features depend on the repository's GitHub plan;
missing/inaccessible rules block authentication. No environment is automatically
created or made permissive by setup.

Manual workflows only become available after a separately approved merge to `main`.
Neither this draft PR nor its CI trusts the feature branch or runs those workflows.
Never change the provider to trust a feature branch as a shortcut.

## Proposed least-privilege GCP additions

| Principal | Scope / permissions |
|---|---|
| `synthetic-quality-deployer@…` | Custom project role: only `run.jobs.create`, `run.jobs.get` |
| Deployer | Artifact Registry Reader on `synthetic-quality-phase1` only |
| Deployer | Service Account User on the existing `synthetic-quality-runtime@…` only |
| `synthetic-quality-executor@…` | Custom role bound on **the exact job**, with `run.jobs.get`, `run.jobs.run`, `run.executions.get`, `run.executions.list` |
| Each dedicated identity | Workload Identity User for the exact repository principalSet in its own isolated pool |

Full service-account domain: `gcp-intelligent-data-quality.iam.gserviceaccount.com`.
Pools: `synthetic-quality-deploy` and `synthetic-quality-execute`, provider `github`.
Custom role IDs: `syntheticQualityJobDeployer` and `syntheticQualityJobExecutor`.
The complete concrete trust/resource/binding proposal is `authorization-plan.json`.

No identity receives Owner/Editor, Cloud Build submission, job update/delete,
execution overrides/cancellation, IAM policy writes or broad storage access.
The existing build/runtime accounts and IAM bindings are preserved. Google-managed
service-agent roles must remain in place; this script does not repair them broadly.
The protected bucket `gcp-idq-data-984592102391` is excluded; the runtime keeps its
existing planned-bucket permissions. No recovery receipt is read or changed here.

Deployment creation permissions necessarily apply at project scope before a job
exists. Execution access is job-scoped; no undocumented resource-name condition or
project-wide execute grant is used to avoid the owner completion step below.

## One-time owner procedure — review before any authorization

Retrieve branch `deploy/github-oidc` into a fresh Cloud Shell directory and review
the draft PR commit. Do not overwrite an existing checkout or receipts:

```bash
git clone --branch deploy/github-oidc --single-branch \
  https://github.com/chandrakanth02512-ctrl/ml-synthetic-data-quality.git \
  "$HOME/ml-synthetic-data-quality-oidc"
cd "$HOME/ml-synthetic-data-quality-oidc"
python3 deploy/oidc/bootstrap.py plan
```

`plan` is offline and makes no cloud calls. It prints the exact resources, audience,
CEL conditions, roles and additive bindings for owner review.

After recording the verified image, configuring GitHub gates, and reviewing current
project IAM/organization restrictions, the owner may run this **read-only** check:

```bash
python3 deploy/oidc/bootstrap.py check
```

The check needs a signed-in Cloud Shell human with sufficient read permissions; it
rejects credential overrides and wrong/inactive project or project number. It
verifies the successful image against Build and Artifact Registry. Existing pools,
providers, service accounts, custom roles and bindings are inspected. Missing
resources are distinguished from permission/API/transport failures. Unexpected
trust, extra providers, dedicated-account keys, broader grants, disabled resources
or role drift stop without repair. Other principals' existing bindings are preserved.
An incomplete/disabled image pin blocks this check, with no changes made.

**Only after new explicit owner approval**, the proposed authorization command is:

```bash
python3 deploy/oidc/bootstrap.py apply --approve WIF-SETUP-984592102391
```

This is review material, not authorization to run it now. It enables only missing
`iamcredentials.googleapis.com`/`sts.googleapis.com` after the explicit gate; other
required APIs must already be enabled. It creates only confirmed-missing dedicated
roles/accounts/pools/providers, rechecks before creation, and adds only missing
unconditional planned bindings. Existing matching resources are reused. Drift or
conditional binding ambiguity stops rather than broadens/replaces policies. No
keys, builds, jobs or workloads are created by authorization setup. IAM propagation
can require waiting and another read-only check; no broad grant is an error workaround.

### Execution authorization when the job is absent

An IAM policy cannot be bound to a nonexistent job. Initial setup therefore prints
`EXECUTION AUTHORIZATION PENDING` if the reviewed job is absent. The executor is
not granted project-wide execution as a workaround. After an approved deployment
creates the exact reviewed job, the owner reruns the **same idempotent** setup command
to add its exact-job binding. This is the one-time authorization procedure's owner
completion step, not a recurring requirement for subsequent runs. Deployer cannot
grant that access itself. No execution is authorized until the step is completed.

## Future manual operations — still separate approvals

After code/config merge and owner setup, manually dispatch **GCP manual job
deployment** from `main`. Confirm the configured digest and deployment action;
the `gcp-deploy` reviewers must approve. The workflow checks the registry, then
creates or verifies the single job by digest. It never updates an unrelated job
or executes it. Record the job UID from its summary for the independent execution gate.

Job settings remain: 1 task, parallelism 1, 1 vCPU, 1 GiB, timeout 600 seconds,
zero retries, 256 training / 100 evaluation rows, seeds 42/123, `smoke-001`.

Only after exact-job execution access is complete, manually dispatch **GCP manual
smoke execution** from `main`. Confirm the same digest, reviewed job UID and execution
action; the `gcp-execute` reviewers must approve separately. It validates the complete
job configuration, empty execution history and etag, then sends one asynchronous
execution request with no overrides. Shared non-cancelling concurrency serializes
the two workflows. A later duplicate dispatch sees history and stops.

The execution workflow reports **REQUESTED**, not completed or ML-successful. It
does not wait 600 seconds using short credentials. Inspect completion/artifacts
separately with authorized read access. A timeout or lost write acknowledgement
requires inspection, not automatic resubmission. No POST is retried by this code.
Global at-most-once execution is not guaranteed against concurrent out-of-band
owners or deleted history; use one operator and preserve all evidence.

## Testing, remaining limits and cost

Offline tests use mocks/fakes and block network/process calls. They cover trust
isolation, main/repository/owner/event restrictions, audiences, reviewer gates,
setup idempotence/drift, missing versus denied, disabled config, successful-image
validation, digest-only creation, UID/etag/history guards, no write retries and no
execution from deployment. Original application files and dependency constraints
remain unchanged. The new tools use Python standard library only. The offline CI
has no OIDC permission or GCP auth step.

Live WIF token exchange, GitHub gate availability, IAM propagation, quotas, policy
constraints, registry digest, job readiness/execution and actual spend remain
unverified. Parent/group/deny/PAB grants require owner review in addition to direct
binding inspection. APIs/identities alone do not start compute. Later execution and
retained artifacts/logs may be billed; neither the earlier ₹100 experiment estimate
nor the ₹500 alert is a hard spending cap. No new build is part of this procedure.

Review current effective permissions, applicable organization/deny policies and
the Google-managed Cloud Run service agent before approving setup or enabling the
configuration. Direct policy inspection does not establish inherited effective
permissions or prove a future WIF exchange will work. See `verification.md` for the
offline checks and precise remaining limits.

References: [Google deployment-pipeline federation](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines),
[GitHub OIDC claims](https://docs.github.com/en/actions/reference/security/oidc),
[GitHub environment protection](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments),
[Cloud Run access](https://docs.cloud.google.com/run/docs/securing/managing-access),
[short-lived auth action](https://github.com/google-github-actions/auth/blob/7c6bc770dae815cd3e89ee6cdf493a5fab2cc093/README.md).
