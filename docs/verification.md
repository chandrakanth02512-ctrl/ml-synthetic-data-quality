# Phase 1 verification evidence

Verified in hosted **Codex Cloud on October 29, 2026**, Linux x86_64,
Python **3.12.14**, pytest **9.2.0**, pytest-cov **7.1.0**, Ruff **0.17.3**.
No GCP provisioning/build/execution commands were run. No real transaction/customer
data, service-account keys, or generated run artifacts were added to Git.

## Baseline before changes

Base commit: `01f0315` (`Build verified Phase 1 synthetic data quality and anomaly detection`).

| Check | Observed result |
|---|---|
| `python -m pytest --cov=synthetic_quality --cov-report=term-missing` | **82 passed in 6.93s**, 0 failures |
| Coverage, statements plus branches | **94.50%** rounded (343/360 statements; 69/76 branches) |
| `python -m ruff check src tests` | All checks passed |
| `python -m ruff format --check src tests` | 17 files already formatted |
| `python -m pip check` | No broken requirements found |

The original generator, data contract, validation, feature engineering, rule baseline,
model, evaluation, pipeline CLI, existing tests and example reports are unchanged.
The cloud entry point calls `run_pipeline` with its existing `PipelineConfig`.

## Extended suite

Install from `pyproject.toml` with the optional `cloud,dev` extras. The new
`requirements-cloud.txt` records 32 runtime package versions used for the container
and Linux checks; the original `requirements-lock.txt` remains unchanged.

```bash
.venv/bin/python -m pytest --cov=synthetic_quality --cov-report=term-missing --cov-fail-under=94.5
.venv/bin/python -m ruff check src tests
.venv/bin/python -m ruff format --check src tests
.venv/bin/python -m pip check
```

| Check | Observed result |
|---|---|
| Complete suite | **164 passed in 39.07s**, 0 failures |
| Original tests | All **82** retained and passing |
| New cloud tests | **82** passing: 44 configuration/path cases, 38 job/storage cases |
| Coverage, statements plus branches | **95.88%** (497/515 statements; 108/116 branches) |
| New configuration and GCS adapter coverage | **100%** each |
| New cloud orchestration coverage | **98%**; module-launch guard not covered in parent process |
| Ruff lint | All checks passed |
| Ruff formatting | 22 files already formatted |
| Dependency consistency | No broken requirements found, on host and in container |
| Combined runtime/development constraints | Pip resolution dry-run passed with both snapshots |
| Cloud Build / CI files | YAML parsed successfully; remote services not executed |

Coverage measures the combined statement and branch opportunities, not branch-only
coverage. Results are environment-specific. The original CLI subprocess executes in
a separate process and is not included in parent-process coverage. CI enforces a
94.5% combined coverage floor and requires no GCP credentials or secrets.

Tests use the real generation/training/report pipeline and a GCS fake that enforces
generation preconditions. They cover valid/invalid environment and CLI configuration,
unsafe paths, bounded sizes, independent seeds, exact artifact inventory, temporary
directory cleanup, missing credentials, permission errors, transient upload failures,
lost upload/completion acknowledgements, create conflicts, matching partial-run
recovery, changed run configuration/runtime, upload corruption, malformed manifests,
missing/changed completed artifacts and new run namespaces. Retries never overwrite
or delete existing objects. Test fixtures block accidental construction of live GCS
clients; fake clients require no ADC or paid API calls.

## Container checks in Codex Cloud

The actual `Dockerfile` built successfully on the local hosted-Codex Docker daemon,
using the session CA as an ephemeral BuildKit secret and verified HTTPS package
downloads. Runtime dependency consistency passed inside the image. The container
entry point's `--help` check passed.
With networking disabled and a deliberately nonexistent ADC file path, the cloud
entry point exited **1** with a clear authentication error and keyless-identity
guidance; no cloud connection was attempted.

An offline smoke test ran the original pipeline **inside that container** with:

- Network disabled, read-only root filesystem and a temporary `/tmp` filesystem.
- Non-root UID **10001**, one CPU, **1 GiB** memory, 256 training / 100 evaluation rows.
- Both data-quality reports passing and saved-model reload verification passing.
- No GCS calls; container outputs were temporary and removed with the container.
- Session CA mount, `.git` and tests absent from the runtime application directory.

For that separate small-data experiment, Isolation Forest precision was **0.25**,
recall **0.20**, F1 **0.2222**, FP **3**, FN **4** (five injected anomalies). This is not
an improvement claim or a replacement for the original 1,000-row evaluation with
recall **0.1400**; the experiments have different training/evaluation sizes.

## Not yet verified

The container smoke test is not a managed GCP execution. Actual Cloud Build image
publication, Artifact Registry access, Cloud Run Jobs deployment/execution, keyless
ADC via attached service account, GCS IAM/preconditions/retries over Google's API,
regional availability/quotas, organization policies, resource sizing and billing
controls remain pending. The CI workflow is prepared; hosted GitHub Actions results
must be inspected separately on the pull request.

Follow the [deployment and cost guide](gcp-phase1.md) only after explicit approval.
No continuously running service or later-phase GCP products are included.
