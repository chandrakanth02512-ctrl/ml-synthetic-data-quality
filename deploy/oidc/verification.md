# OIDC review evidence and limits

Prepared in Codex Cloud on October 30, 2026. No GCP/IAM setup, Cloud Build,
Cloud Run deployment/execution or manual workflow dispatch was performed.
Updated October 31, 2026 (Asia/Calcutta), with owner-run source success and inactive
solo-owner approval preparation. Live operations remain separately approved.

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

- **146 OIDC/source/review tests passed.** Fakes/mocks cover separate trust pools, exact numeric
  IDs/main/manual event/workflow/environment/audiences, least-privilege grants,
  additive idempotent setup, disabled configuration, existing-resource drift,
  cancelled-build rejection, Build/registry digest equality, approval rules,
  owner attestation for unreported bypass settings, job UID/etag/history,
  digest-only creation, and no automatic POST retry or execution from deployment.
- The **18 archive tests** additionally cover supplied hash decoding, full pinned
  file fixtures, modified/missing/extra/duplicate inputs, generation and Build drift,
  MD5-only rejection, unsafe paths/links/devices/modes, compressed/decompressed size
  bounds and hidden trailing content. All archives are local synthetic fixtures,
  not copies of the live consumed source. No extraction or activation is performed.
- New regressions cover disabled-by-default solo policy, exact owner reviewer,
  protected/current main, owner-only initial dispatch, disabled read-only bootstrap,
  GET and fixed read-only IAM-query RPCs, distinct permission/API/missing errors
  and complete pagination. Prepared solo-owner support remains inactive; design
  approval does not authorize live operations.
- The owner has now explicitly approved the solo-owner **design only**. Configuration
  selects that mode with its design authorization true; `enabled` and both bypass
  attestations remain false. No live settings were changed.
- **34 collector integration tests** exercise all nine categories with synthetic
  private-policy fixtures, expected missing versus denied/disabled access, locally
  unset versus unsupported constraints, redaction, incomplete ancestry, VPC/quotas,
  GitHub gates, credential failure stop, request caching/no retry and forbidden
  commands. One additional reader regression verifies fixed getIamPolicy read
  payloads, and the CLI summary/invalid-local-evidence paths are covered. No real
  GCP preflight was run by Codex.
- Nine new regressions verify the documented v1 numeric-project locations read,
  method failures versus permissions/availability, explicit per-request quota
  attribution, operator `serviceusage.services.use`, redacted ErrorInfo consumer
  relationships, contradictory enabled/SERVICE_DISABLED evidence and distinct
  quota-user/IAM/security-policy denials. No new writes, retries or API enablement.
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
independently queried them. The owner then supplied matching submitted/resolved
source generation `1793365313077688`, archive MD5/SHA-256, exact Docker arguments,
environment and substitutions. The metadata is consistent with the pinned recovery
configuration, and is recorded in `source-evidence.json`. The owner subsequently
downloaded the exact GCS generation and reported a successful actual archive
comparison: `ARCHIVE_CONTENTS_MATCH_PINNED_APPLICATION`, 19 files, exact application
commit, build ID, source archive hash and image digest. That owner-run result is
recorded with its limitations. Codex did not inspect the live bytes and does not
claim independent signed provenance. Source comparison is complete on that evidence
basis; deployment and execution remain disabled.

Solo-owner approval is the explicitly approved design, with its authorization true.
`approval-options.md` recommends explicitly authorized self-approval for this
personal project while retaining separate exact-owner reviewer gates. Support is
prepared but inactive because deployment and bypass gates remain false; no live
protection setting was changed. `authorization-review.md` provides the single
automated command plus exact private manual checks and stop conditions.

Before activation, retain the completed image/source verification and review
GitHub environment features/reviewers/main-only rules/disabled bypass, effective
permissions (including inherited/group/deny/PAB policies), organization restrictions,
existing runtime/registry access and the Cloud Run service agent. Direct policy
inspection is not proof of all effective authorization. A revision tag is a
declared source label, not independent proof of source contents.

No live token exchange, IAM propagation, GitHub approval cycle, quotas, job
reconciliation, execution success or actual spend is established by offline tests.
If the job is absent, initial owner authorization deliberately defers the exact-job
execution binding until a separately approved deployment creates it.

## Owner preflight follow-up

The owner supplied the previous collector summary: 41 PASS / 7 BLOCKED /
18 EXPECTED MISSING / 9 UNVERIFIED. Those are historical counts, not new live
results. [readiness-assessment.md](readiness-assessment.md) classifies each unresolved
finding and required evidence. The authorized GitHub integration now independently
reports main unprotected, while detailed settings remain inaccessible. No settings
were changed. Corrected GCP reads still require the existing authenticated Cloud
Shell; no independent GCP conclusions are claimed. The approved solo-owner design
remains inactive with `enabled` and both bypass attestations false.
