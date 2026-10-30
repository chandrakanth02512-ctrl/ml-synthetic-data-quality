# GitHub recovery integration verification

Verified in Codex Cloud on 30 October 2026, Python 3.12.14. The GitHub connector
confirmed main at `34cc3d46b662a9f0560c3ff846d68b327d52e4b8` before preparation.
An isolated snapshot of all 46 original files matched GitHub's Git blob hashes;
the deployment integration tests check those hashes again on every CI run.

Observed local results:

| Check | Result |
|---|---|
| Recovery, Stage 1 and repository integration unit tests | **51 passed**, including temporary-HOME receipt preservation and mocked cloud/Git/process calls |
| Original ML suite | **82 passed** in the existing tested environment; core source matches the pinned blobs |
| Ruff lint across original source/tests and deployment tools | Passed |
| Ruff format | 31 files already formatted |
| Existing tested environment `pip check` | No broken requirements |
| Recovery dependency audit | Standard library and sibling modules only; no new third-party requirements |
| Existing application files | All 46 unchanged, including source, tests, constraints, Dockerfile, Cloud Build YAML and original CI |
| New-file credential/key signature and private-artifact scan | Passed; only explicit source/docs/workflow files selected |

The local environment does not have the optional Google Cloud Storage dependency.
The unchanged application CI installs `.[cloud,dev]` using both existing constraints
files, runs dependency/lint/format checks and the complete 164-test offline suite
with its coverage floor. The separate deployment workflow runs all 51 deployment
tests without cloud credentials or package installation. Consult the draft PR's
current CI results for those hosted checks.

The deployment branch is tool source only. Any later approved build uses a new
isolated application clone checked out detached at the full pinned SHA. Tests
explicitly verify detached checkout and reject a changed application revision
before a recovery receipt or build submission is created.

No GCP build, job creation/execution, infrastructure/IAM change, key creation or
live preflight was performed during integration. No real Cloud Shell attempt marker
was copied, edited, overwritten or deleted. The exact cancelled build remains an
explicit historical exception; one replacement still requires separate approval.

Authentication, live policies/permissions, quota capacity, original receipt
availability, registry/job inventory, actual submission/deployment and costs remain
subject to the read-only Cloud Shell check and later separately approved actions.
No ZIPs, virtual environments, historical scripts, credentials or runtime receipts
are included in the deployment branch.
