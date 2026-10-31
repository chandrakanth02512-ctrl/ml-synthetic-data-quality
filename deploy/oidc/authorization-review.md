# Read-only authorization review before any live setup

**No authorization to change resources/settings.** Run these reads only in already
authenticated Cloud Shell. Codex currently has no GCP identity/API egress. The
solo-owner design is now approved in configuration; keep `enabled` and both bypass
attestations false. Do not enable an API, log in as
another identity, impersonate an account, grant roles or change a perimeter to make
a check pass. Keep raw organization/IAM outputs private and outside this repository.

Every section uses explicit project coordinates. Ancestor/policy IDs below must
come from the successful inventory, not guesses. `--quiet` suppresses interactive
API-enablement prompts. A failed check blocks the affected capability; it does not
prove a missing resource or authorize a workaround.

## One automated procedure — run this first

From a fresh checkout of this PR's exact reviewed commit in Google Cloud Shell:

```bash
python3 deploy/oidc/authorization_preflight.py check
```

The collector uses existing `gcloud` human authentication and existing `gh`
authentication, if available. It does not initiate login, impersonation, API
enablement, repairs or workloads. It verifies the active local GCP project before
any cloud read; select the intended Cloud Shell account/project yourself before
running it. A mismatch is reported, never corrected. Missing GitHub authentication
blocks that category while independent GCP checks continue. A GCP authentication
failure stops further cloud probes. No token is printed, put in process arguments,
downloaded or stored by the collector. Normal existing CLI credential handling is
not replaced or inspected for secret contents.

It gathers all nine categories below automatically using read-only REST calls.
Resource requests have no retry loop and cache successes/failures; the same failed
request is not reissued for another check. POSTs are restricted to the read-only
`testIamPermissions` and `getIamPolicy` RPCs, with fixed query payloads; all other
remote operations are GET. `gcloud` is used only for local configuration/identity
inspection and one captured access-token request. `gh api` is GET-only. Completed
source/build verification is not repeated: only the recorded owner-run evidence is
checked for consistency. Artifact Registry receives an immutable metadata read,
not a Docker download, build or new tag selection.

The console output is a JSON **summary**, with every check classified exactly:

| Status | Meaning |
|---|---|
| PASS | The stated read/check succeeded; this never grants deployment approval. |
| EXPECTED MISSING | A structured not-found for a planned future resource, or a supported locally unset policy. Effective/default policy is still inspected. |
| BLOCKED | Authentication, permission/API availability, configuration mismatch or incompatible controls prevent verification/progress. |
| UNVERIFIED | Missing visibility, unsupported constraint, conditional policy, network context, quota headroom or another fact that safe reads cannot establish automatically. |

Reasons distinguish API disabled, permission denied, supported policy locally
unset and unsupported/not-applicable constraint. Category status is the most
restrictive contained result. No raw IAM/org/deny/PAB/VPC/quotas inventories, private
principals, rule expressions, credential output or CLI stderr are printed or
written to files. Administrator identifiers are retained only in process memory.
Share the summary for review; do not commit raw inventories or private exports.

Exit **0** means collection has only PASS/EXPECTED MISSING, **1** includes
UNVERIFIED, and **2** includes BLOCKED/local prerequisite failure. None authorizes
live changes. These are deliberate report outcomes, not a request to rerun until
green. Partial visibility remains explicit: project Owner does not automatically
grant ancestor/deny/PAB/VPC visibility; conditional restrictions need administrator
evaluation; quota limits do not establish current usage/headroom; protected-main
flags do not prove all bypass rules; an API may omit administrator-bypass state.
The collector never attests an omitted setting and does not perform a live WIF
exchange. The detailed commands below remain private manual fallbacks for an
authorized administrator to resolve precisely those limitations.

## 1. Active human, billing, APIs, ancestry and existing resources

```bash
gcloud auth list --filter=status:ACTIVE --format='table(account,status)'
gcloud config get-value auth/impersonate_service_account
gcloud config get-value auth/credential_file_override
gcloud config get-value auth/access_token_file
gcloud projects describe gcp-intelligent-data-quality --format=json --quiet
gcloud billing projects describe gcp-intelligent-data-quality --format=json --quiet
gcloud projects get-ancestors gcp-intelligent-data-quality --format=json --quiet
gcloud services list --enabled --project=gcp-intelligent-data-quality --format=json --quiet

gcloud storage buckets describe gs://synthetic-quality-984592102391-data-as1 \
  --project=gcp-intelligent-data-quality --format=json --quiet
gcloud storage buckets describe gs://synthetic-quality-984592102391-source-as1 \
  --project=gcp-intelligent-data-quality --format=json --quiet
gcloud artifacts repositories describe synthetic-quality-phase1 \
  --location=asia-south1 --project=gcp-intelligent-data-quality --format=json --quiet
gcloud run jobs list --region=asia-south1 \
  --project=gcp-intelligent-data-quality --format=json --quiet

# Reads existing image, identities, roles, trust and direct bindings; works disabled.
python3 deploy/oidc/bootstrap.py check
```

Require one intended human, no credential override/impersonation, ACTIVE project
number `984592102391`, and billing enabled. Reconfirm planned private Mumbai buckets,
repository/image and no unexpected job. The unrelated `gcp-idq-data-984592102391`
bucket is excluded. A matching existing job is inspected, never updated or executed.
The already verified source remains an owner-run result, not signed provenance.

Existing service APIs include IAM, Service Usage, Resource Manager, Storage,
Artifact Registry, Run, Build and Logging. Inspect availability of Organization
Policy, Policy Troubleshooter, Access Context Manager, IAM Credentials and STS too.
Any disabled inspection API is **BLOCKED / API DISABLED**, not an empty inventory.
Missing IAM Credentials/STS is a pending separately approved setup addition.

## 2. Effective permissions and scoped allow policies

`review_read.py` permits GET and read-only `:testIamPermissions`/`:getIamPolicy`
POSTs only. Permission-query bodies and version-3 policy-read bodies are fixed;
no policy write payload is accepted. It uses
the current human's short-lived token in memory, rejects auth overrides through
the existing owner preflight, never writes credentials, and never enables APIs.
The POSTs below are permission queries, not IAM changes. Describe a resource before
testing its permissions: some permission-test APIs return empty results for absence.

```bash
gcloud projects get-iam-policy gcp-intelligent-data-quality --format=json --quiet
gcloud iam service-accounts describe \
  synthetic-quality-runtime@gcp-intelligent-data-quality.iam.gserviceaccount.com \
  --project=gcp-intelligent-data-quality --format=json --quiet
gcloud iam service-accounts describe \
  synthetic-quality-build@gcp-intelligent-data-quality.iam.gserviceaccount.com \
  --project=gcp-intelligent-data-quality --format=json --quiet

python3 deploy/oidc/review_read.py test \
  'https://cloudresourcemanager.googleapis.com/v1/projects/gcp-intelligent-data-quality:testIamPermissions' \
  --permissions resourcemanager.projects.get resourcemanager.projects.getIamPolicy \
  resourcemanager.projects.setIamPolicy iam.roles.create iam.serviceAccounts.create \
  iam.workloadIdentityPools.create iam.workloadIdentityPoolProviders.create \
  serviceusage.services.enable run.jobs.get run.jobs.create

python3 deploy/oidc/review_read.py test \
  'https://iam.googleapis.com/v1/projects/gcp-intelligent-data-quality/serviceAccounts/synthetic-quality-runtime@gcp-intelligent-data-quality.iam.gserviceaccount.com:testIamPermissions' \
  --permissions iam.serviceAccounts.actAs iam.serviceAccounts.getIamPolicy iam.serviceAccounts.setIamPolicy
python3 deploy/oidc/review_read.py test \
  'https://iam.googleapis.com/v1/projects/gcp-intelligent-data-quality/serviceAccounts/synthetic-quality-build@gcp-intelligent-data-quality.iam.gserviceaccount.com:testIamPermissions' \
  --permissions iam.serviceAccounts.actAs
python3 deploy/oidc/review_read.py test \
  'https://artifactregistry.googleapis.com/v1/projects/gcp-intelligent-data-quality/locations/asia-south1/repositories/synthetic-quality-phase1:testIamPermissions' \
  --permissions artifactregistry.repositories.get artifactregistry.repositories.getIamPolicy \
  artifactregistry.repositories.setIamPolicy

gcloud storage buckets get-iam-policy gs://synthetic-quality-984592102391-data-as1 \
  --project=gcp-intelligent-data-quality --format=json --quiet
gcloud storage buckets get-iam-policy gs://synthetic-quality-984592102391-source-as1 \
  --project=gcp-intelligent-data-quality --format=json --quiet
gcloud artifacts repositories get-iam-policy synthetic-quality-phase1 \
  --location=asia-south1 --project=gcp-intelligent-data-quality --format=json --quiet
gcloud iam roles describe syntheticQualityObjects --project=gcp-intelligent-data-quality --format=json --quiet
```

Require each requested operator permission to be returned; Owner is not used as a
substitute for an effective test. These test the **current caller**, not a service
account named in the URL. Project-level tests are not proof of resource-scoped
authority; inspect/test each existing scope before its future IAM change. A new
account's own policy cannot be tested until it exists. Setup may still fail due to
constraints/propagation/concurrent changes; missing authority stops for review.

For each folder/org in the verified ancestor list, inspect inherited allow policy:

```bash
gcloud resource-manager folders get-iam-policy FOLDER_ID --format=json --quiet
gcloud organizations get-iam-policy ORGANIZATION_ID --format=json --quiet
```

Require current runtime object-create/get only on the planned data bucket, existing
build grants preserved, and Google-managed service-agent bindings:
`service-984592102391@serverless-robot-prod.iam.gserviceaccount.com` with
`roles/run.serviceAgent`, and the existing Cloud Build service agent with
`roles/cloudbuild.serviceAgent`. Do not repair or grant these to user accounts.

Use Policy Troubleshooter, if already enabled and readable, for the following
principal/resource/permission matrix. The first exact command illustrates the form:

```bash
gcloud policy-troubleshoot iam \
  '//cloudresourcemanager.googleapis.com/projects/gcp-intelligent-data-quality' \
  --principal-email=synthetic-quality-deployer@gcp-intelligent-data-quality.iam.gserviceaccount.com \
  --permission=run.jobs.create --project=gcp-intelligent-data-quality --format=json --quiet
```

| Principal | Full resource | Permissions to inspect individually |
|---|---|---|
| Deployer | `//cloudresourcemanager.googleapis.com/projects/gcp-intelligent-data-quality` | `run.jobs.create`, `run.jobs.get` |
| Deployer | `//iam.googleapis.com/projects/gcp-intelligent-data-quality/serviceAccounts/synthetic-quality-runtime@gcp-intelligent-data-quality.iam.gserviceaccount.com` | `iam.serviceAccounts.actAs` |
| Deployer | `//artifactregistry.googleapis.com/projects/gcp-intelligent-data-quality/locations/asia-south1/repositories/synthetic-quality-phase1` | `artifactregistry.repositories.downloadArtifacts`, `artifactregistry.dockerimages.get` |
| Runtime | `//storage.googleapis.com/projects/_/buckets/synthetic-quality-984592102391-data-as1/objects/synthetic-quality/run-manifests/smoke-001/request.json` | `storage.objects.create`, `storage.objects.get` |
| Executor, only after the job exists | `//run.googleapis.com/projects/gcp-intelligent-data-quality/locations/asia-south1/jobs/synthetic-quality-phase1-smoke` | `run.jobs.get`, `run.jobs.run`, `run.executions.get`, `run.executions.list` |

Before setup, missing deployer/executor identities/grants are expected **PENDING**,
not an instruction to create them. Substitute the full service-account emails from
`settings.py` for the principal column. Repeat after separately approved setup.
Conditional/group/deny/PAB explanations must be fully visible and applicable. An
unknown, unsupported resource/permission or access-tuple error is **UNVERIFIED**.
Troubleshooter does not diagnose VPC Service Controls or prove an OIDC exchange.

## 3. IAM deny and Principal Access Boundary restrictions

List deny policies at the project and **every** verified ancestor attachment point:

```bash
gcloud iam policies list \
  --attachment-point=cloudresourcemanager.googleapis.com/projects/984592102391 \
  --kind=denypolicies --project=gcp-intelligent-data-quality --format=json --quiet
gcloud iam policies list \
  --attachment-point=cloudresourcemanager.googleapis.com/folders/FOLDER_ID \
  --kind=denypolicies --project=gcp-intelligent-data-quality --format=json --quiet
gcloud iam policies list \
  --attachment-point=cloudresourcemanager.googleapis.com/organizations/ORGANIZATION_ID \
  --kind=denypolicies --project=gcp-intelligent-data-quality --format=json --quiet
```

Inspect complete rules/exceptions/conditions for each returned policy and its
affected permissions. If list output omits detail, use its exact `name` with
`review_read.py get https://iam.googleapis.com/v2/RETURNED_POLICY_NAME`.
Also review PAB policies/bindings applicable to these principals via the successful
ancestor inventory and IAM's org administrator, using reads:

```bash
gcloud iam principal-access-boundary-policies list \
  --organization=ORGANIZATION_ID --location=global \
  --project=gcp-intelligent-data-quality --format=json --quiet
gcloud iam policy-bindings list --organization=ORGANIZATION_ID --location=global \
  --project=gcp-intelligent-data-quality --format=json --quiet
gcloud iam policy-bindings list --folder=FOLDER_ID --location=global \
  --project=gcp-intelligent-data-quality --format=json --quiet
gcloud iam policy-bindings list --project=gcp-intelligent-data-quality --location=global \
  --format=json --quiet
```

Inspect each binding's policy reference, target principal set and conditions, and
the referenced PAB rules; use the exact returned `name` with a read of
`https://iam.googleapis.com/v3/RETURNED_NAME` if details are omitted. Inspect all
applicable bindings, including any administered outside this project hierarchy.
Incomplete visibility is blocked, not "no deny".
An allow grant cannot override deny, PAB, organization policy or perimeter rules.

## 4. Organization policy and ancestor/custom constraints

```bash
python3 deploy/oidc/review_read.py get \
  'https://orgpolicy.googleapis.com/v2/projects/984592102391/constraints' --pages=constraints
gcloud org-policies list --project=gcp-intelligent-data-quality --format=json --quiet
gcloud org-policies describe gcp.resourceLocations --effective \
  --project=gcp-intelligent-data-quality --format=json --quiet
gcloud org-policies describe iam.workloadIdentityPoolProviders --effective \
  --project=gcp-intelligent-data-quality --format=json --quiet
gcloud org-policies describe iam.disableServiceAccountCreation --effective \
  --project=gcp-intelligent-data-quality --format=json --quiet
gcloud org-policies describe iam.allowedPolicyMemberDomains --effective \
  --project=gcp-intelligent-data-quality --format=json --quiet
```

For **each applicable constraint from the inventory**, repeat the effective describe,
particularly all custom constraints, `iam.managed.allowedPolicyMembers`, domain/
issuer/principal restrictions, endpoint/location rules, CMEK requirements and Run
VPC/Binary Authorization settings. Inspect direct policies on every ancestor too:
`gcloud org-policies list --folder=FOLDER_ID` and `--organization=ORGANIZATION_ID`.
No create, set, reset or delete command is part of this review.

Classify distinctly: inventory excludes a constraint → **UNSUPPORTED / NOT
APPLICABLE**; supported constraint has a direct-policy structured NOT_FOUND →
**LOCALLY UNSET**, then still inspect the effective inherited/default policy;
PERMISSION_DENIED → **BLOCKED / PERMISSION**; SERVICE_DISABLED → **BLOCKED / API**;
transport/invalid response → **UNVERIFIED**. NOT_FOUND alone does not distinguish an
unsupported constraint, absent policy or wrong resource. Expected local absence
must not abort an unexplained shell loop; run each read and record its status.

Require Mumbai resource placement and the global WIF pools/issuer
`https://token.actions.githubusercontent.com` plus external principalSet members to
be compatible with all effective restrictions. Missing ancestor visibility stops.

## 5. VPC Service Controls

If ancestry identifies an organization, use **all** applicable access policies,
including policies scoped to folders/projects:

```bash
gcloud access-context-manager policies list --organization=ORGANIZATION_ID \
  --project=gcp-intelligent-data-quality --format=json --quiet
gcloud access-context-manager perimeters list --policy=ACCESS_POLICY_ID \
  --project=gcp-intelligent-data-quality --format=json --quiet
gcloud access-context-manager levels list --policy=ACCESS_POLICY_ID \
  --project=gcp-intelligent-data-quality --format=json --quiet
```

Review enforced `status`, dry-run `spec`, membership of `projects/984592102391`,
restricted services, bridges, access levels, ingress/egress identities and rules.
Confirm the intended external GitHub-hosted runner → STS/IAM Credentials → Run/AR
path and the job → Storage path are permitted. The runner's network context differs
from Cloud Shell, so a successful Cloud Shell read is insufficient. Do not add an
IP allowlist, bridge or perimeter exception as a workaround. Disabled API or missing
org visibility is blocked; an unfiltered complete empty inventory is distinguishable.
If there is no organization, record that verified ancestry and ask the owner to
confirm applicability; do not infer it from permission failures.

## 6. Regional service availability and quotas

```bash
gcloud run regions list --project=gcp-intelligent-data-quality --format=json --quiet
python3 deploy/oidc/review_read.py get \
  'https://serviceusage.googleapis.com/v1beta1/projects/984592102391/services/run.googleapis.com/consumerQuotaMetrics?view=FULL' \
  --pages=metrics
python3 deploy/oidc/review_read.py get \
  'https://serviceusage.googleapis.com/v1beta1/projects/984592102391/services/iam.googleapis.com/consumerQuotaMetrics?view=FULL' \
  --pages=metrics
python3 deploy/oidc/review_read.py get \
  'https://serviceusage.googleapis.com/v1beta1/projects/984592102391/services/iamcredentials.googleapis.com/consumerQuotaMetrics?view=FULL' \
  --pages=metrics
python3 deploy/oidc/review_read.py get \
  'https://serviceusage.googleapis.com/v1beta1/projects/984592102391/services/sts.googleapis.com/consumerQuotaMetrics?view=FULL' \
  --pages=metrics
```

Require `asia-south1` in service locations and compatible regional job/CPU/memory/
task/execution quotas for one task, 1 vCPU, 1 GiB, 600 seconds, zero retries. Inspect
metric units/dimensions and current usage/headroom in Cloud Console Quotas; a quota
limit alone is not remaining capacity. Check global WIF pool/provider/account/role
limits and token request quotas. Disabled/not-yet-used APIs may have unavailable
quota data; mark pending and recheck after approved setup. Service location support
and quota headroom do not reserve capacity. No Cloud Build quota increase is needed
or requested: the existing immutable image is reused and no build is submitted.

## 7. GitHub environments and protected main

Use the owner's existing GitHub web session or already authenticated `gh`; do not
create a PAT, key or new login to bypass access. These are GETs only:

```bash
gh auth status
gh api --method GET repos/chandrakanth02512-ctrl/ml-synthetic-data-quality/branches/main
gh api --method GET repos/chandrakanth02512-ctrl/ml-synthetic-data-quality/branches/main/protection
gh api --method GET repos/chandrakanth02512-ctrl/ml-synthetic-data-quality/rules/branches/main --paginate
gh api --method GET repos/chandrakanth02512-ctrl/ml-synthetic-data-quality/environments/gcp-deploy
gh api --method GET repos/chandrakanth02512-ctrl/ml-synthetic-data-quality/environments/gcp-deploy/deployment-branch-policies --paginate
gh api --method GET repos/chandrakanth02512-ctrl/ml-synthetic-data-quality/environments/gcp-execute
gh api --method GET repos/chandrakanth02512-ctrl/ml-synthetic-data-quality/environments/gcp-execute/deployment-branch-policies --paginate
```

Inspect classic protection **and** effective rulesets; a classic-protection 404
alone does not prove main is unprotected. Require protected main, applicable CI
checks, no force-push/deletion and reviewed PR-based changes. A sole owner can use
PR-before-merge with CI and zero mandatory peer approvals, then explicitly review
and merge their own PR; requiring another person's review would recreate the same
deadlock. This is a proposed owner-governance choice, not a live settings change.

Current default: non-self reviewer in each environment. Proposed solo-owner mode,
only after separate approval: exactly one User reviewer ID `341606319` in each,
`prevent_self_review: false`, administrator bypass disabled, exactly one branch
policy `main` of type `branch`, no tag/wildcard policy. Owner must attest disabled
bypass if the API omits it. Missing/unavailable protection features block activation.
Runtime solo checks also require protected main's current commit to equal the
dispatched SHA; moved main stops before GCP auth. Full branch-rule governance still
needs this owner review; a boolean protected field does not prove every setting.

## 8. WIF readiness, with no token exchange or setup

```bash
python3 deploy/oidc/bootstrap.py plan
python3 deploy/oidc/bootstrap.py check
gcloud iam workload-identity-pools list --location=global \
  --project=gcp-intelligent-data-quality --format=json --quiet
gcloud iam service-accounts list --project=gcp-intelligent-data-quality --format=json --quiet
```

The existing check describes both exact pools/providers, dedicated accounts/roles,
direct policies, provider issuer/audience/mappings/conditions, extra-provider trust
and user-managed key metadata. Matching resources pass; drift/denied access stops;
missing planned resources are pending separately approved setup. Inspect dedicated
account policies for exact-pool `roles/iam.workloadIdentityUser`, deployer actAs only
on runtime, AR read on the dedicated repo and project create/get only. Executor's
run/get/history grant is **exact-job only**, deferred if the job is absent. Parent
allow/deny/PAB inspection is still required beyond these direct checks.

New planned provider conditions also require `actor_id == '341606319'`. Workflows
and runtime reject any other dispatcher and any rerun (`run_attempt != 1`). On an
ambiguous prior outcome, inspect resources/history and obtain new action approval;
never click rerun as an automatic recovery. No service-account impersonation or
live OIDC/token-exchange test is authorized here. Those remain unverified until an
explicitly approved future authentication step; a valid plan is not a successful
exchange. API enablement, propagation, policy/quotas and provider creation are
separate setup concerns.

## Review result and next approval boundary

Record PASS / EXPECTED MISSING / BLOCKED / UNVERIFIED for every category, with
permission/API/constraint errors kept distinct. No category is globally approved
from a partial direct IAM check. Send only a redacted summary for Codex review.
The solo-owner design approval has been received and recorded; `enabled` and
bypass flags remain false. After reviewing the summary, the next live approval
would explicitly authorize the exact GitHub environment/protected-main changes
identified by the report. GCP authorization additions still require explicitly
scoped approval. Deployment and
execution retain separate manual/environment approvals after readiness is complete.

Official syntax and limits reviewed: [deny inventory](https://docs.cloud.google.com/sdk/gcloud/reference/iam/policies/list),
[IAM troubleshooting](https://docs.cloud.google.com/policy-intelligence/docs/troubleshoot-access),
[effective org policy](https://docs.cloud.google.com/sdk/gcloud/reference/org-policies/describe),
[ancestor inventory](https://docs.cloud.google.com/sdk/gcloud/reference/projects/get-ancestors),
[access policies](https://docs.cloud.google.com/sdk/gcloud/reference/access-context-manager/policies/list),
[perimeters](https://docs.cloud.google.com/sdk/gcloud/reference/access-context-manager/perimeters/list),
[Run regions](https://docs.cloud.google.com/sdk/gcloud/reference/run/regions/list).
