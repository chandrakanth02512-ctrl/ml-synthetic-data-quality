# OIDC review evidence and limits

Prepared in Codex Cloud on October 30, 2026. No GCP/IAM setup, Cloud Build,
Cloud Run deployment/execution or manual workflow dispatch was performed.

## Saved work and source preservation

The interrupted work was resumed in `/workspace/ml-synthetic-data-quality-oidc`.
`git status` and `git diff` were inspected before editing. The saved OIDC files
were also copied to `/tmp/oidc-resume-preserved` before refinement; that temporary
backup is not included in GitHub. Existing recovery work was left in place.

All 46 original repository files match their Git blob hashes at
`34cc3d46b662a9f0560c3ff846d68b327d52e4b8`, including application source, tests,
dependency constraints, Dockerfile, Cloud Build configuration and original CI.
`test_workflows.py` enforces these hashes from `application-baseline.json`.
Only `deploy/oidc/` and three new workflow files are proposed for this PR.
PR #2 and `deploy/stage2-recovery` remain separate and unmerged.

## Offline validation

```bash
python3 -m unittest discover -s deploy/oidc -p 'test_*.py' -v
python3 -m ruff check src tests deploy/oidc
python3 -m ruff format --check src tests deploy/oidc
python3 -m pip check
```

- **78 OIDC tests passed.** Fakes/mocks cover separate trust pools, exact numeric
  IDs/main/manual event/workflow/environment/audiences, least-privilege grants,
  additive idempotent setup, disabled configuration, existing-resource drift,
  cancelled-build rejection, Build/registry digest equality, approval rules,
  owner attestation for unreported bypass settings, job UID/etag/history,
  digest-only creation, and no automatic POST retry or execution from deployment.
- Ruff **0.17.3** lint and formatting passed over `src`, `tests`, `deploy/oidc`.
- Existing Python **3.12.14** environment dependency consistency passed. New
  deployment tools import only standard-library and sibling modules; they install
  no application dependencies or GCP SDK on the workflow runner.
- All workflow YAML files and embedded Python blocks parsed successfully. Tests
  check manual-only deployment triggers, separate approvals, pre-authentication
  guards, matching provider audiences and immutable action references.
- Local actionlint download was blocked by the unavailable session HTTP proxy:
  `curl: (7) Failed to connect to proxy port 8080`. The offline GitHub check runs
  actionlint **1.7.12** using its exact release archive SHA-256 before execution;
  its result and the full existing application CI must be inspected on the PR.
- Only the explicit new source/docs/config/test/workflow files are selected for
  publishing. No credentials, tokens, keys, real recovery receipts, archives,
  environments, generated models/data or private files are included.

CI has read-only GitHub permissions and no OIDC permission or Google auth step.
Deployment/execution workflows are manual only and the checked-in configuration
is disabled with the owner-verified replacement build/digest recorded and both
bypass attestations still `false`.

## Live verification still required

Current environment observations report no outbound identity, no credential
bindings and no GCP API egress. `gcloud` and an outbound identity manifest are
unavailable. No authentication workaround or live GCP command was attempted.
The owner subsequently supplied Cloud Shell verification of replacement build
`fc1ca075-570b-45da-b588-977423c49f0d` and matching immutable image digest
`sha256:35a1e032472e06e4b21c55996b46faa0090deef3a4bdee710f27ddcb85e33116`.
Build and Docker step were `SUCCESS`, with the expected build identity/source
bucket. These public values are recorded in disabled configuration. Codex has not
independently queried them. Source provenance/consumed bytes remain unverified;
see `source-provenance.md` for the code audit, missing evidence and exact read-only
metadata request. A valid image identity does not satisfy source-content proof.

The approval policy is unchanged. The sole-owner constraint and alternatives are
reviewed in `approval-options.md`; the current non-self-review rule prevents the
owner approving their own dispatch. No alternative has been applied.

Before activation, the owner must review that image, the source provenance,
GitHub environment features/reviewers/main-only rules/disabled bypass, effective
permissions (including inherited/group/deny/PAB policies), organization restrictions,
existing runtime/registry access and the Cloud Run service agent. Direct policy
inspection is not proof of all effective authorization. A revision tag is a
declared source label, not independent proof of source contents.

No live token exchange, IAM propagation, GitHub approval cycle, quotas, job
reconciliation, execution success or actual spend is established by offline tests.
If the job is absent, initial owner authorization deliberately defers the exact-job
execution binding until a separately approved deployment creates it.
