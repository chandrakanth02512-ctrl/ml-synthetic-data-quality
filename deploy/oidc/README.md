# GitHub Actions → GCP, without service-account keys

**Review package only. No live GCP/IAM changes, workflow dispatch, deployment,
execution or Cloud Build submission were performed.** The proposed authorization
must be reviewed and explicitly approved by the project owner before setup.

The application source pin remains
`34cc3d46b662a9f0560c3ff846d68b327d52e4b8`; the owner-run archive comparison has
succeeded on the evidence basis below. These workflows never build an image and
never access, delete or reset the Cloud Shell recovery receipts. PR #2 remains a
separate unmerged recovery-tools change; this authentication proposal does not
depend on its tools or run another recovery attempt.

## Recorded image and owner-verified source; activation still disabled

The owner verified the successful replacement Build and Docker step in Cloud Shell,
matched its published digest against Artifact Registry, and confirmed the dedicated
build account and recovery source bucket. The reviewed `config.json` now records:

```json
{
  "enabled": false,
  "image_digest": "sha256:35a1e032472e06e4b21c55996b46faa0090deef3a4bdee710f27ddcb85e33116",
  "replacement_build_id": "fc1ca075-570b-45da-b588-977423c49f0d"
}
```

The full immutable image URI is
`asia-south1-docker.pkg.dev/gcp-intelligent-data-quality/synthetic-quality-phase1/synthetic-quality@sha256:35a1e032472e06e4b21c55996b46faa0090deef3a4bdee710f27ddcb85e33116`.
This is owner-reported live verification; Codex has not independently queried GCP.
No digest is guessed and no tag/latest fallback exists. Both manual workflows stop
before requesting GCP credentials while disabled; authorization `apply` also stops.
The rest of `config.json` fixes the planned data bucket and `smoke-001` namespace.
Both `environment_bypass_disabled_attested` entries also default to `false`.
`approval_mode` remains `independent` and `solo_owner_approval_authorized` remains
`false`; the proposed solo-owner policy cannot activate under these defaults.

To recheck these exact values in authenticated Google Cloud Shell using reads:

```bash
gcloud builds describe fc1ca075-570b-45da-b588-977423c49f0d \
  --project=gcp-intelligent-data-quality --region=asia-south1 --format=json

gcloud artifacts docker images list \
  asia-south1-docker.pkg.dev/gcp-intelligent-data-quality/synthetic-quality-phase1 \
  --project=gcp-intelligent-data-quality --include-tags --format=json

gcloud artifacts docker images describe \
  'asia-south1-docker.pkg.dev/gcp-intelligent-data-quality/synthetic-quality-phase1/synthetic-quality@sha256:35a1e032472e06e4b21c55996b46faa0090deef3a4bdee710f27ddcb85e33116' \
  --project=gcp-intelligent-data-quality --format=json
```

Retain the exact **replacement build ID**, **`sha256:` digest**, and **full immutable
image URI**. Exclude cancelled build `6f75c5dd-cc3a-4d47-9c9d-43f51fa3c4ba`.
Require Build and Docker-step `SUCCESS`, the declared replacement revision tag
`34cc3d46b662a9f0560c3ff846d68b327d52e4b8-recovery1`, and equality between the Build
published digest and Artifact Registry digest. Any differing provenance/settings
requires review, not weakened validation or another build.

The revision tag is a declared label, not independent proof of the source archive.
The source audit and exact outstanding evidence are in
[source-provenance.md](source-provenance.md). Build success, a matching image digest,
the correct build identity and source bucket establish different facts from source
content identity. Do not activate if the consumed source cannot be tied to the pin.
The supplied resolved object/generation and archive hashes are consistent. The
owner downloaded that exact generation and successfully ran the offline verifier:
**19 files matched the pinned application**, with archive SHA-256
`84d69e14c3934d8965edd6c734417710816cb20a615d713841bc21c68612519e`.
`source-evidence.json` records the owner-run result and its limits: no independent
Codex archive inspection or signed provenance is claimed. Tags/hashes alone were
not used as Git-content proof. No source re-verification is currently requested.

The verified public values are recorded through this draft PR. Keep `enabled: false`
and both bypass attestations `false` until the remaining checks and owner-approved
activation are complete.

## Trust and approval boundaries

| Check | Exact value |
|---|---|
| Project / number | `gcp-intelligent-data-quality` / `984592102391` |
| Region | `asia-south1` |
| Repository ID | `1436792250` |
| Repository owner ID | `341606319` |
| Dispatching actor ID | `341606319` |
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
Provider CEL now also restricts the dispatcher to the exact owner ID. Workflows
and runtime reject reruns (`run_attempt != 1`); inspect ambiguous outcomes and
request new action approval rather than rerunning a workflow.

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

For this sole-owner personal project, the recommended prepared alternative is
explicit owner approval in both separate environments, with self-review permitted
only after a separate reviewed policy authorization. No setting is changed now;
see [approval-options.md](approval-options.md). Independent review remains the
default; `solo-owner` mode requires only the exact owner reviewer and verifies
protected main/current dispatch commit before GCP authentication.

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

With the image recorded and deployment still disabled, the owner may run this
**read-only** check while reviewing authorization prerequisites:

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
An incomplete image pin blocks this check; a valid pin with `enabled: false` permits
read-only inspection. Disabling deployment never needs to be reversed for a read.
Run the additional exact checks in [authorization-review.md](authorization-review.md)
for effective IAM, ancestors, deny/PAB policies, org constraints, VPC Service
Controls, quotas, GitHub protection and WIF readiness. Those checks do not enable
APIs or repair permissions. Missing access or policy visibility remains blocking.

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
constraints, independent image/provenance verification, job readiness/execution and
actual spend remain unverified by Codex. Parent/group/deny/PAB grants require owner
review in addition to direct
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
