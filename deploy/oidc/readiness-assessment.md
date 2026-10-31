# Authorization readiness after the owner preflight

Reviewed October 31, 2026. **Not ready for live authorization or deployment.**
This assessment changes verification code/documentation only. No live settings,
APIs, IAM, resources, receipts or deployment flags were changed.

## Evidence and limits

The owner reported 41 PASS, 7 BLOCKED, 18 EXPECTED MISSING and 9 UNVERIFIED checks
at `d92a09ce9a87f83bc47fc8e941c6722b685409e6`. Context and Artifact Registry passed.
Those counts describe the previous run, not the corrected collector. Individual
private inventories were not supplied or committed.

The successful Build, immutable image and owner-run 19-file archive comparison
remain unchanged. This is completed owner evidence, not independent signed
provenance. The collector does not query Cloud Build, download the archive or
repeat its comparison.

The authorized GitHub integration independently returned pinned main and
`protected: false`. Detailed classic protection returned HTTP 403,
`Resource not accessible by integration`. Environment and effective-ruleset reads
were rejected by the connector's endpoint allowlist (HTTP 400), before usable
settings evidence was obtained. That does not establish missing environments.
No new credentials were created. Codex has no authorized GCP identity/API egress;
corrected GCP results need an owner-side read.

## Unresolved findings

| Finding | Classification and evidence | Smallest next read / decision |
|---|---|---|
| Main protection | **BLOCKED: actual configuration gap.** Branch read reports `protected: false`. | Owner reads Rulesets/Branches settings for active main rules, required CI/PR changes, blocked force-push/deletion and no administrative bypass. Changes need separate approval. |
| Organization Policy | **BLOCKED: inspection failure, cause unproven.** HTTP 403 `SERVICE_DISABLED` conflicts with target enabled-services inventory. The v2 constraints endpoint is documented and valid. | Corrected requests specify target `X-Goog-User-Project`. Inspect redacted `affected_service` and `error_consumer`. Other-project attribution is plausible, not proven for the old run. If the same target still conflicts, investigate dependencies/propagation/error evidence privately; do not blindly enable APIs. |
| Ancestor deny / PAB | **BLOCKED: insufficient visibility.** Project reads succeeded; ancestor HTTP 403 `PERMISSION_DENIED`. No evidence yet of an actual denying policy. | Existing authorized organization/security admin reads all ancestor deny/PAB bindings and applicable principal-side PAB, including outside this hierarchy; evaluates relevant rules/exceptions/conditions privately. |
| Effective IAM | **UNVERIFIED: incomplete evidence.** Current-caller tests/direct grants do not establish every future principal's effective access. | Retain successful current-caller/actAs results. Resolve relevant inherited/group/conditional/deny/PAB effects for the tuples below. Nonexistent future identities cannot pass live access yet. |
| VPC Service Controls | **BLOCKED: API/permission/applicability unresolved.** Access Context Manager unavailable; no proof of a perimeter or its absence. | Corrected inventory distinguishes disabled target API, quota-user failure, IAM denial and explicit security-policy violation. Admin reviews complete organization/scoped policies, enforced/dry-run perimeters and intended paths. |
| Cloud Run locations | **UNVERIFIED: confirmed incorrect previous method.** Old v2 locations-list URL returned HTTP 404 `PRIVATE_ERROR_OMITTED`. | Corrected collector uses documented v1 numeric-project endpoint. Endpoint failures remain UNVERIFIED, permission failures BLOCKED; neither establishes Mumbai unavailability. |
| Quota / capacity | **UNVERIFIED: insufficient evidence.** Limits do not establish current regional usage/headroom. | Read Console Quotas & System Limits for Run Mumbai CPU/memory/jobs/tasks/executions and global IAM/WIF/token quotas. Require one 1-vCPU/1-GiB task's headroom. Availability is not a reservation. |
| GitHub environments | **BLOCKED: existing CLI auth unavailable; settings unknown.** Integration cannot expose settings endpoints. | Use already signed-in owner UI below. No PAT/login/token creation. Native solo-owner approvals are feasible for this public personal repo, subject to actual feature/settings checks. |
| Planned WIF resources/identities | **EXPECTED MISSING** where structured absence was observed; overall readiness **UNVERIFIED**. | Review `authorization-plan.json`; do not create resources. Existing drift/403 must not be relabeled expected absence. |
| Token exchange / propagation | **UNVERIFIED: intentionally not performed.** | Future explicitly approved setup/authentication step; offline tests or a valid plan cannot prove a live exchange. |

## One owner-side diagnostic procedure

In already authenticated Cloud Shell, retrieve this PR's **reviewed exact commit**
into a fresh directory (the completion report supplies the literal SHA):

```bash
(
  set -eu
  reviewed_commit='REPLACE_WITH_REVIEWED_PR3_COMMIT'
  review_dir="$(mktemp -d "$HOME/gcp-authorization-review.XXXXXXXX")"
  git clone --branch deploy/github-oidc --single-branch \
    https://github.com/chandrakanth02512-ctrl/ml-synthetic-data-quality.git "$review_dir"
  git -C "$review_dir" checkout --detach "$reviewed_commit"
  test "$(git -C "$review_dir" rev-parse HEAD)" = "$reviewed_commit"
  cd "$review_dir"
  python3 deploy/oidc/authorization_preflight.py check
)
```

Only the fresh local checkout is written. No previous checkout/receipt is touched;
no deletion/cleanup command is included. Keep shell tracing off. Summary output
omits accounts, ancestor IDs, policies, conditions and credentials. Diagnostic
metadata shows only the known target quota project, allowlisted service and
consumer relationship `TARGET_PROJECT` / `OTHER_PROJECT_REDACTED` / `NOT_REPORTED`.

Require already selected active project `gcp-intelligent-data-quality`, existing
human authentication and `serviceusage.services.use` on the target quota project.
The collector tests this permission and reports failures; it never changes gcloud
config, ADC, billing or APIs, switches quota projects, logs in or grants anything.
Exit 0/1/2 means only PASS/EXPECTED MISSING / includes UNVERIFIED / includes BLOCKED;
none authorizes deployment or an automatic retry. Share only the summary.

If existing `gh` auth is absent, GCP collection continues and GitHub stays BLOCKED.
Do not install credentials. Using the owner's existing GitHub web session, read
(do not save/toggle/create):

1. Settings → Rules → Rulesets and Settings → Branches: effective active main
   protections, PR-before-merge, required CI, blocked force-push/deletion and no
   administrative bypass. Zero mandatory peer approvals avoids a sole owner
   needing to approve their own PR; explicit owner review/merge remains required.
2. Settings → Environments → **each** `gcp-deploy` and `gcp-execute`: existence,
   feature availability, exactly reviewer `chandrakanth02512-ctrl` (ID `341606319`),
   **Prevent self-review off**, **Allow administrators to bypass off**, selected
   deployment branches exactly `main`, type branch, no tags/wildcards. Missing
   settings/features remain blockers; an omitted API field proves nothing.

Return only setting conclusions. Leave both bypass attestations false. Self-review
provides a separate human action, not independent review or protection against
owner-account compromise. The solo-owner design is already approved, but inactive.

## Administrator-only visibility checks

Use an **already authorized** organization/security administrator. This is not a
request to grant the project owner broad access. Actual ancestor IDs must come
from trusted ancestry. Private read commands are in
[authorization-review.md](authorization-review.md), sections 2–6. Do not enable
unavailable APIs. An administrator may use their existing approved audit context;
the owner collector remains pinned to the target quota project.

| Existing responsible administrator / read | Required existing scoped permissions |
|---|---|
| Resource/IAM admin: ancestry and inherited allow | `resourcemanager.folders.get`, `resourcemanager.organizations.get`, applicable folder/organization `getIamPolicy`; project equivalents where needed. |
| Org-policy admin: supported/custom/effective policy | `orgpolicy.constraints.list`, `orgpolicy.policies.get/list`, `orgpolicy.customConstraints.get/list` if relevant. |
| IAM security admin: deny at every ancestor | `iam.denypolicies.list/get` at those attachment points. |
| IAM security admin: applicable PAB rules/bindings | `iam.policybindings.list/get` at relevant scopes; `iam.principalaccessboundarypolicies.list/get` at relevant organizations, including applicable principal-side restrictions. |
| Perimeter admin: complete VPC SC review | `accesscontextmanager.policies.list/get`, `accesscontextmanager.servicePerimeters.list/get`, `accesscontextmanager.accessLevels.list/get` at relevant organization/policies. |
| Service/quota viewer: current usage/headroom | `serviceusage.services.get/list`, `serviceusage.quotas.get`; existing monitoring visibility where usage metrics require it. No increase. |

Use IAM Policy Troubleshooter **only if already accessible/enabled**, with existing
policy/group visibility, for these specific principal/resource/permission tuples:

- Current operator → project: setup permissions already queried by the collector;
  → existing runtime and build accounts: `iam.serviceAccounts.actAs`; → dedicated
  repository: policy permissions required by later separately approved setup.
- Existing runtime → planned data bucket: `storage.objects.create/get`; existing
  Cloud Run service agent → dedicated repository: image download access. Review
  object-dependent conditions against the planned smoke namespace.
- Future deployer → project `run.jobs.create/get`, runtime `actAs`, repository
  read/download; future executor → **exact job** `run.jobs.get/run` and
  `run.executions.get/list`. These remain planned until separately authorized
  creation/job-scoped binding. Do not impersonate or grant access to fabricate PASS.

Evaluate allow/deny/PAB plus group/conditional uncertainty in the intended request
context. Incomplete results stay UNVERIFIED. IAM analysis does not evaluate all org
or perimeter restrictions. Separately review GitHub runner → STS/IAM Credentials →
Run/Artifact Registry and job → Storage paths, including scoped policies, enforced
and dry-run membership, bridges, restricted services and ingress/egress. Successful
Cloud Shell reads cannot prove runner access. Return only redacted conclusions and
scope-completeness; do not send principal lists, rule text, inventories or tokens.

## Next authorization boundary

First review corrected summary and private administrator/UI conclusions. The known
live gap is main protection. The next possible live decision is **explicit approval
of an exact main-protection and two-environment settings plan**, after current
features/settings are known. Solo-owner design approval need not be requested again.
No settings change is performed by this PR. WIF/IAM/API setup, merge/activation,
deployment and execution remain separately approved operations. All deployment
enablement and bypass attestation flags remain false.

Official references: [Run v1 locations](https://docs.cloud.google.com/run/docs/reference/rest/v1/projects.locations/list),
[quota project and caller permission](https://docs.cloud.google.com/docs/quotas/set-quota-project),
[v2 supported constraints](https://docs.cloud.google.com/organization-policy/reference/rest/v2/projects.constraints/list),
[IAM troubleshooting limits](https://docs.cloud.google.com/policy-intelligence/docs/troubleshoot-access),
[PAB binding inventory](https://docs.cloud.google.com/iam/docs/reference/rest/v3/organizations.locations.policyBindings/list),
[Access Context Manager inventory](https://docs.cloud.google.com/access-context-manager/docs/reference/rest/v1/accessPolicies/list),
[GitHub protection indicator](https://docs.github.com/en/rest/branches/branches#list-branches),
[environment approvals](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments).
