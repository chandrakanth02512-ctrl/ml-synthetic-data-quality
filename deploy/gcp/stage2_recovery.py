"""Explicit, one-attempt recovery for one known cancelled Cloud Build.

Importing performs no I/O. check/status never write receipts or mutate GCP.
submit/deploy are separately gated future operator actions, not automatic retries.
"""

import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlencode

from preflight import PROJECT, REGION, REV, Context, api, gc, pages
from stage1 import require
from stage2 import (
    IMAGE,
    REPO_URL,
    env_vars,
    git,
    image_uri,
    job_url,
    reviewed_environment,
    validate_job,
)

KNOWN_CANCELLED = "6f75c5dd-cc3a-4d47-9c9d-43f51fa3c4ba"
RECOVERY_TAG = REV + "-recovery1"
SUBMIT_APPROVAL = "APPROVE-ONE-REPLACEMENT-" + KNOWN_CANCELLED
DEPLOY_APPROVAL = "APPROVE-DEPLOY-REPLACEMENT-DIGEST"


def state_root():
    return Path.home() / ".local/state/synthetic-quality-phase1"


def original_path():
    return state_root() / f"{PROJECT}-build-{REV}.attempt"


def recovery_path():
    return state_root() / f"{PROJECT}-build-recovery1-{KNOWN_CANCELLED}-{REV}.attempt"


def event_path(event):
    require(
        event
        in {
            "cancelled-build",
            "submitted",
            "submission-uncertain",
            "verified-build",
            "job-created",
        },
        "Unknown receipt event",
    )
    return state_root() / f"{PROJECT}-recovery1-{KNOWN_CANCELLED}-{REV}-{event}.json"


def read_receipt(path, optional=False):
    require(not path.is_symlink(), "Receipt symlink is not accepted: " + str(path))
    if not path.exists():
        require(
            optional, "Required receipt missing; do not recreate or delete markers: " + str(path)
        )
        return None
    require(
        path.is_file() and path.stat().st_size <= 262144, "Receipt is not a bounded regular file"
    )
    raw = path.read_bytes()
    data = json.loads(raw)
    require(isinstance(data, dict), "Invalid receipt JSON")
    return data, hashlib.sha256(raw).hexdigest()


def original_receipt():
    data, digest = read_receipt(original_path())
    require(
        all(
            data.get(k) == v
            for k, v in {"project": PROJECT, "revision": REV, "action": "build"}.items()
        ),
        "Original attempt marker does not match reviewed project/revision/action",
    )
    return {"path": str(original_path()), "sha256": digest}


def recovery_receipt(c, original, required=False):
    result = read_receipt(recovery_path(), optional=not required)
    if result is None:
        return None
    data = result[0]
    expected = {
        "schema_version": 1,
        "project": PROJECT,
        "region": REGION,
        "revision": REV,
        "action": "one-replacement-build",
        "cancelled_build_id": KNOWN_CANCELLED,
        "replacement_tag": RECOVERY_TAG,
        "original_receipt_sha256": original["sha256"],
        "operator": c.account,
    }
    require(
        all(data.get(k) == v for k, v in expected.items()),
        "Recovery receipt identity mismatch; stop for review",
    )
    require(isinstance(data.get("created_at"), str), "Recovery timestamp missing")
    dt.datetime.fromisoformat(data["created_at"].replace("Z", "+00:00"))
    return data


def write_new_receipt(path, data):
    """Exclusive, append-only-by-file receipts; never truncate an existing file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    require(not path.is_symlink(), "Receipt symlink is not accepted")
    try:
        with path.open("x") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except FileExistsError:
        raise RuntimeError(
            "Receipt already exists; never delete/overwrite to force another attempt: " + str(path)
        ) from None
    print("PRESERVED NEW RECEIPT:", path)


def same_or_new_receipt(path, data):
    previous = read_receipt(path, optional=True)
    if previous is None:
        write_new_receipt(path, data)
    else:
        require(previous[0] == data, "Existing evidence differs; it will not be overwritten")


def repo_url(c):
    return f"https://artifactregistry.googleapis.com/v1/projects/{PROJECT}/locations/{REGION}/repositories/{c.repository}"


def registry_images(c):
    # Includes untagged images; every page must be readable before absence is inferred.
    return pages(repo_url(c) + "/dockerImages", "dockerImages")


def all_jobs(c):
    return pages(
        f"https://run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/jobs", "jobs"
    )


def validate_build_metadata(c, b, tag):
    require(bool(re.fullmatch(r"[0-9a-f-]{36}", b.get("id", ""))), "Build ID missing/invalid")
    require(b.get("projectId") == PROJECT, "Build project mismatch")
    require(
        b.get("name")
        in {f"projects/{p}/locations/{REGION}/builds/{b['id']}" for p in (PROJECT, c.number)},
        "Build resource region/name mismatch",
    )
    expected = {"_TAG": tag, "_REGION": REGION, "_REPOSITORY": c.repository, "_IMAGE": IMAGE}
    require(
        all(b.get("substitutions", {}).get(k) == v for k, v in expected.items()),
        "Build substitutions/source revision tag mismatch",
    )
    require(
        b.get("serviceAccount")
        in {f"projects/{p}/serviceAccounts/{c.build}" for p in (PROJECT, c.number)},
        "Build identity mismatch",
    )
    require(b.get("options", {}).get("logging") == "CLOUD_LOGGING_ONLY", "Build logging mismatch")
    require(
        b.get("options", {}).get("machineType") == "E2_STANDARD_2" and b.get("timeout") == "1200s",
        "Build sizing/timeout mismatch; review instead of relaxing",
    )
    require(
        b.get("source", {}).get("storageSource", {}).get("bucket") == c.source,
        "Build source bucket mismatch",
    )

    # Cloud Build responses can retain templated fields; allow only their exact
    # expansion using the verified substitutions, never an arbitrary image name.
    def expanded(value):
        for k, v in {"PROJECT_ID": PROJECT, **b.get("substitutions", {})}.items():
            value = value.replace("${" + k + "}", str(v)).replace("$" + k, str(v))
        return value

    require(
        [expanded(v) for v in b.get("images", [])] == [image_uri(c) + ":" + tag],
        "Declared output image mismatch",
    )
    require(
        len(b.get("steps", [])) == 1
        and b["steps"][0].get("name") == "gcr.io/cloud-builders/docker",
        "Unexpected build steps",
    )
    require(not b.get("buildTriggerId"), "Triggered builds are outside this manual recovery")


def cancelled_build(c):
    b = gc("builds", "describe", KNOWN_CANCELLED, "--region=" + REGION, json_output=True)
    require(b.get("id") == KNOWN_CANCELLED, "Exact cancelled build ID not returned")
    validate_build_metadata(c, b, REV)
    require(
        b.get("status") == "CANCELLED" and b["steps"][0].get("status") == "CANCELLED",
        "Known build and Docker step must both remain CANCELLED",
    )
    require(
        not b.get("results", {}).get("images"),
        "Cancelled build reports published images; stop for investigation",
    )
    return b


def relevant_builds(c):
    inventory = gc("builds", "list", "--region=" + REGION, json_output=True)
    relevant = [
        b
        for b in inventory
        if b.get("id") == KNOWN_CANCELLED
        or b.get("substitutions", {}).get("_REPOSITORY") == c.repository
        or any(x.startswith(image_uri(c)) for x in b.get("images", []))
        or any(
            x.get("name", "").startswith(image_uri(c))
            for x in b.get("results", {}).get("images", [])
        )
    ]
    ids = [b.get("id") for b in relevant]
    require(
        len(ids) == len(set(ids)) and ids.count(KNOWN_CANCELLED) == 1,
        "Build inventory incomplete/duplicated or known cancelled build absent",
    )
    require(
        len(relevant) <= 2,
        "More than one additional application build exists; "
        "no recovery submission/deployment allowed",
    )
    return relevant


def operator_permissions(c):
    permissions = {
        "cloudbuild.builds.create",
        "cloudbuild.builds.get",
        "cloudbuild.builds.list",
        "run.jobs.create",
        "run.jobs.get",
        "run.jobs.list",
    }
    result = api(
        "POST",
        f"https://cloudresourcemanager.googleapis.com/v1/projects/{PROJECT}:testIamPermissions",
        {"permissions": sorted(permissions)},
    )
    require(
        permissions <= set(result.get("permissions", [])),
        "Operator build/job project permissions missing: "
        + ", ".join(sorted(permissions - set(result.get("permissions", [])))),
    )
    query = urlencode(
        [("permissions", "storage.objects.create"), ("permissions", "storage.objects.get")]
    )
    result = api(
        "GET",
        f"https://storage.googleapis.com/storage/v1/b/{c.source}/iam/testPermissions?" + query,
    )
    require(
        {"storage.objects.create", "storage.objects.get"} <= set(result.get("permissions", [])),
        "Operator source-bucket upload/read permission missing",
    )
    print(
        "PASS: operator build/job permissions and source upload/read test; "
        "both actAs tests are checked by Context"
    )


def snapshot(c):
    original = original_receipt()
    marker = recovery_receipt(c, original)
    old = cancelled_build(c)
    inventory = relevant_builds(c)
    other = [b for b in inventory if b["id"] != KNOWN_CANCELLED]
    replacement = None
    if other:
        require(
            marker is not None,
            "Additional build exists without this recovery receipt; no automatic adoption",
        )
        replacement = gc(
            "builds", "describe", other[0]["id"], "--region=" + REGION, json_output=True
        )
        require(replacement.get("id") == other[0]["id"], "Replacement build identity changed")
        validate_build_metadata(c, replacement, RECOVERY_TAG)
        created = dt.datetime.fromisoformat(
            replacement.get("createTime", "").replace("Z", "+00:00")
        )
        approved = dt.datetime.fromisoformat(marker["created_at"].replace("Z", "+00:00"))
        require(created >= approved, "Replacement predates local approval receipt; stop")
        source_object = replacement.get("source", {}).get("storageSource", {}).get("object", "")
        require(
            source_object.startswith("source/recovery-" + KNOWN_CANCELLED + "/"),
            "Replacement source-staging namespace mismatch",
        )
    submitted = read_receipt(event_path("submitted"), optional=True)
    if submitted is not None:
        require(
            marker is not None and replacement is not None,
            "Submitted receipt cannot be reconciled with server history",
        )
        require(
            submitted[0].get("replacement_build_id") == replacement["id"],
            "Submitted receipt and server build ID mismatch",
        )
    return {
        "original_receipt": original,
        "recovery_receipt": marker,
        "cancelled_build": old,
        "replacement_build": replacement,
        "images": registry_images(c),
        "jobs": all_jobs(c),
        "planned_job": api("GET", job_url(c), missing=True),
    }


def check_ready(c):
    s = snapshot(c)
    require(
        s["recovery_receipt"] is None,
        "Replacement attempt already reserved/consumed; use recovery-status, never submit again",
    )
    require(s["replacement_build"] is None, "Additional build already exists")
    require(not s["images"], "Dedicated repository is not empty, including untagged images")
    require(
        not s["jobs"] and s["planned_job"] is None,
        "Cloud Run Job already exists in target region; stop",
    )
    print(
        "PASS: exact known cancelled build/step; no published image; no Mumbai jobs; "
        "original receipt fingerprint preserved"
    )
    return s


def verify_success(c, s):
    b = s["replacement_build"]
    require(
        s["recovery_receipt"] is not None and b is not None,
        "Replacement receipt/build not both present",
    )
    require(
        b.get("status") == "SUCCESS" and b["steps"][0].get("status") == "SUCCESS",
        "Replacement is not successful; inspect only, no third build is allowed",
    )
    images = [
        x
        for x in b.get("results", {}).get("images", [])
        if x.get("name") == image_uri(c) + ":" + RECOVERY_TAG
    ]
    require(
        len(images) == 1 and re.fullmatch(r"sha256:[0-9a-f]{64}", images[0].get("digest", "")),
        "Replacement published digest unavailable",
    )
    digest = images[0]["digest"]
    require(
        len(s["images"]) == 1 and s["images"][0].get("uri") == image_uri(c) + "@" + digest,
        "Registry inventory differs from replacement build digest",
    )
    d = gc(
        "artifacts",
        "docker",
        "images",
        "describe",
        image_uri(c) + ":" + RECOVERY_TAG,
        json_output=True,
    )
    require(
        d.get("image_summary", {}).get("digest") == digest,
        "Replacement tag differs from build-result digest",
    )
    print(
        "PASS: successful replacement",
        b["id"],
        "digest",
        digest,
        "; cancelled build is excluded from image selection",
    )
    return digest


def recovery_image(c):
    # Shared by Stage 2 and the separately gated Stage 3; never selects 'latest'.
    return verify_success(c, snapshot(c))


def submit_once(c):
    check_ready(c)
    require(
        input("Only after NEW explicit one-replacement approval, type " + SUBMIT_APPROVAL + ": ")
        == SUBMIT_APPROVAL,
        "Replacement build not approved",
    )
    source = tempfile.mkdtemp(prefix="synthetic-quality-recovery-source-", dir=Path.home())
    git("clone", "--no-checkout", "--branch", "main", "--single-branch", REPO_URL, source)
    git("checkout", "--detach", REV, cwd=source)
    require(
        git("rev-parse", "HEAD", cwd=source) == REV
        and not git("status", "--porcelain", cwd=source),
        "Source differs from reviewed clean revision",
    )
    require(
        git("ls-remote", "origin", "refs/heads/main", cwd=source).split()[0] == REV,
        "Current main moved; stop for review",
    )
    previous_cwd = os.getcwd()
    os.chdir(source)
    try:
        print(gc("meta", "list-files-for-upload"))
        require(
            input("Review source upload allowlist; type UPLOAD-REVIEWED: ") == "UPLOAD-REVIEWED",
            "Upload not approved",
        )
        require(not git("status", "--porcelain", cwd=source), "Source changed before submission")
        s = check_ready(c)
        marker = {
            "schema_version": 1,
            "project": PROJECT,
            "region": REGION,
            "revision": REV,
            "action": "one-replacement-build",
            "cancelled_build_id": KNOWN_CANCELLED,
            "replacement_tag": RECOVERY_TAG,
            "original_receipt_sha256": s["original_receipt"]["sha256"],
            "operator": c.account,
            "created_at": dt.datetime.now(dt.UTC).isoformat(),
        }
        # Durable exclusive reservation is written BEFORE any submission. Never remove it.
        write_new_receipt(recovery_path(), marker)
        write_new_receipt(
            event_path("cancelled-build"),
            {"original_receipt": s["original_receipt"], "build": s["cancelled_build"]},
        )
        require(
            original_receipt() == s["original_receipt"],
            "Original receipt changed; attempt remains reserved and will not submit",
        )
        try:
            b = gc(
                "builds",
                "submit",
                ".",
                "--async",
                "--config=cloudbuild.yaml",
                "--region=" + REGION,
                "--machine-type=e2-standard-2",
                "--timeout=1200s",
                "--suppress-logs",
                f"--service-account=projects/{PROJECT}/serviceAccounts/{c.build}",
                f"--gcs-source-staging-dir=gs://{c.source}/source/recovery-{KNOWN_CANCELLED}",
                f"--substitutions=_REGION={REGION},_REPOSITORY={c.repository},_IMAGE={IMAGE},_TAG={RECOVERY_TAG}",
                json_output=True,
                timeout=240,
            )
            require(
                b.get("id") != KNOWN_CANCELLED
                and bool(re.fullmatch(r"[0-9a-f-]{36}", b.get("id", ""))),
                "Submission acknowledgement lacks a distinct build ID",
            )
        except Exception:
            write_new_receipt(
                event_path("submission-uncertain"),
                {
                    "project": PROJECT,
                    "cancelled_build_id": KNOWN_CANCELLED,
                    "meaning": (
                        "Submission failed or acknowledgement was lost. Attempt consumed; "
                        "inspect server history. Never resubmit."
                    ),
                },
            )
            raise
        write_new_receipt(
            event_path("submitted"),
            {
                "project": PROJECT,
                "cancelled_build_id": KNOWN_CANCELLED,
                "replacement_build_id": b["id"],
                "replacement_tag": RECOVERY_TAG,
                "response": b,
            },
        )
        # Persist acknowledgement before validation, so an unexpected response can
        # never erase evidence or invite a blind resubmission.
        validate_build_metadata(c, b, RECOVERY_TAG)
        require(
            original_receipt() == s["original_receipt"],
            "Original receipt changed after submission; preserve evidence and stop",
        )
        print(
            "SUBMITTED ONE REPLACEMENT:",
            b["id"],
            ". Use recovery-status; no polling, job creation or execution was performed.",
        )
    finally:
        os.chdir(previous_cwd)


def deploy_replacement(c):
    s = snapshot(c)
    digest = verify_success(c, s)
    require(
        not [
            j
            for j in s["jobs"]
            if j.get("name") != job_url(c).removeprefix("https://run.googleapis.com/v2/")
        ],
        "Unrelated regional jobs require review",
    )
    if s["planned_job"] is not None:
        validate_job(c, s["planned_job"], digest)
        require(
            s["planned_job"].get("executionCount", 0) == 0
            and not pages(job_url(c) + "/executions", "executions"),
            "Existing job has executions; stop",
        )
        print("MATCH: existing reviewed job; no changes or execution")
        return
    require(not s["jobs"], "Job inventory inconsistent with planned-job absence")
    require(
        input("Only after separate digest/deployment approval, type " + DEPLOY_APPROVAL + ": ")
        == DEPLOY_APPROVAL,
        "Job creation not approved",
    )
    s = snapshot(c)
    require(
        verify_success(c, s) == digest and s["planned_job"] is None and not s["jobs"],
        "State changed before create; stop",
    )
    same_or_new_receipt(
        event_path("verified-build"),
        {
            "project": PROJECT,
            "revision": REV,
            "cancelled_build_id": KNOWN_CANCELLED,
            "replacement_build_id": s["replacement_build"]["id"],
            "digest": digest,
            "build": s["replacement_build"],
        },
    )
    print(
        gc(
            "run",
            "jobs",
            "create",
            c.job,
            "--region=" + REGION,
            "--image=" + image_uri(c) + "@" + digest,
            "--service-account=" + c.runtime,
            "--tasks=1",
            "--parallelism=1",
            "--cpu=1",
            "--memory=1Gi",
            "--task-timeout=600s",
            "--max-retries=0",
            "--labels=deployment=synthetic-quality-phase1",
            "--set-env-vars=" + ",".join(k + "=" + v for k, v in env_vars(c).items()),
            timeout=300,
        )
    )
    j = api("GET", job_url(c))
    validate_job(c, j, digest)
    require(j.get("executionCount", 0) == 0, "Unexpected execution history; stop")
    same_or_new_receipt(
        event_path("job-created"),
        {
            "project": PROJECT,
            "job": j["name"],
            "digest": digest,
            "replacement_build_id": s["replacement_build"]["id"],
        },
    )
    print(
        "JOB CREATED BY IMMUTABLE DIGEST. It has NOT been executed. "
        "Stage 3 is not authorized by recovery."
    )


def main():
    commands = {"recovery-check", "recovery-status", "recovery-submit", "recovery-deploy"}
    require(
        len(sys.argv) == 2 and sys.argv[1] in commands,
        "Usage: python3 stage2.py "
        + "|".join(sorted(commands))
        + "; original build-deploy/deploy-only are disabled in this recovery package",
    )
    c = Context()
    reviewed_environment(c)
    operator_permissions(c)
    action = sys.argv[1]
    if action == "recovery-check":
        check_ready(c)
        print("RECOVERY PREFLIGHT PASSED (read-only). No new approval is implied.")
    elif action == "recovery-status":
        s = snapshot(c)
        print(json.dumps(s, indent=2, sort_keys=True))
        if s["replacement_build"] is not None and s["replacement_build"].get("status") == "SUCCESS":
            verify_success(c, s)
        print(
            "READ-ONLY STATUS FINISHED. Active/failed/cancelled/ambiguous replacement "
            "means inspect and stop; never submit another."
        )
    elif action == "recovery-submit":
        submit_once(c)
    else:
        deploy_replacement(c)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError) as e:
        sys.exit("STOP: " + str(e))
