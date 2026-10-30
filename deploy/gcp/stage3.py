"""One approved smoke execution; verification can be rerun without execution."""

import csv
import hashlib
import io
import json
import subprocess
import sys

from preflight import PREFIX, PROJECT, REGION, RUN_ID, Context, api, gc, pages
from stage1 import require
from stage2 import approved_image, job_url, once_marker, reviewed_environment, validate_job

ARTIFACTS = {
    "data/training.csv": "raw-data",
    "data/evaluation.csv": "raw-data",
    "data/evaluation_truth.csv": "raw-data",
    "reports/data_quality.json": "quality-reports",
    "reports/data_quality.csv": "quality-reports",
    "reports/evaluation.json": "evaluation-reports",
    "reports/evaluation.csv": "evaluation-reports",
    "reports/predictions.csv": "evaluation-reports",
    "models/isolation_forest.joblib": "models",
    "models/model_metadata.json": "models",
    "models/rule_baseline.json": "models",
    "reports/run_manifest.json": "run-manifests",
}


def executions(c):
    return pages(job_url(c) + "/executions", "executions")


def content(c, name, generation=None):
    # Bytes only. Never deserialize executable joblib/pickle data in Cloud Shell.
    uri = "gs://" + c.data + "/" + name
    if generation is not None:
        uri += "#" + str(generation)
    p = subprocess.run(
        [
            "gcloud",
            "--quiet",
            "--project=" + PROJECT,
            "--account=" + c.account,
            "storage",
            "cat",
            uri,
        ],
        capture_output=True,
        check=False,
        timeout=120,
    )
    require(p.returncode == 0, "Artifact read failed; stop and inspect GCS permissions/generation")
    return p.stdout


def verify(c, e, digest):
    require(
        e.get("taskCount") == 1 and e.get("succeededCount") == 1, "Expected one successful task"
    )
    require(
        not e.get("failedCount", 0)
        and not e.get("cancelledCount", 0)
        and not e.get("retriedCount", 0),
        "Execution failed, cancelled or retried",
    )
    require(bool(e.get("completionTime")), "Execution not complete")
    # Inspect the immutable execution template too, in case the job changed between checks.
    validate_job(
        c,
        {
            "name": f"projects/{PROJECT}/locations/{REGION}/jobs/{c.job}",
            "labels": {"deployment": "synthetic-quality-phase1"},
            "terminalCondition": {"state": "CONDITION_SUCCEEDED"},
            "template": {
                "taskCount": e.get("taskCount"),
                "parallelism": e.get("parallelism"),
                "template": e.get("template", {}),
            },
        },
        digest,
    )
    name = f"{PREFIX}/run-manifests/{RUN_ID}/completion.json"
    manifest = json.loads(content(c, name))
    require(manifest.get("status") == "complete", "Completion marker missing/invalid")
    records = manifest.get("artifacts", {})
    require(set(records) == set(ARTIFACTS), "Completion artifact inventory mismatch")
    config = manifest.get("request", {}).get("config", {})
    require(
        config.get("project_id") == PROJECT
        and config.get("bucket") == c.data
        and config.get("run_id") == RUN_ID
        and config.get("prefix") == PREFIX
        and config.get("training_rows") == 256
        and config.get("evaluation_rows") == 100
        and config.get("training_seed") == 42
        and config.get("evaluation_seed") == 123,
        "Completion request differs from approved smoke config",
    )
    data = {}
    for local, category in ARTIFACTS.items():
        r = records[local]
        expected = f"{PREFIX}/{category}/{RUN_ID}/" + local.split("/")[-1]
        require(r.get("name") == expected, "Unexpected artifact object path")
        from urllib.parse import quote

        live = api(
            "GET",
            f"https://storage.googleapis.com/storage/v1/b/{c.data}/o/" + quote(expected, safe=""),
        )
        require(
            str(live.get("generation")) == str(r.get("generation")),
            "Current object generation differs from completion record: " + expected,
        )
        b = content(c, expected, r["generation"])
        require(
            len(b) == r.get("size_bytes") and hashlib.sha256(b).hexdigest() == r.get("sha256"),
            "Generation/hash/size mismatch: " + expected,
        )
        data[local] = b
    for local, count in (("data/training.csv", 256), ("data/evaluation.csv", 100)):
        require(
            len(list(csv.DictReader(io.StringIO(data[local].decode())))) == count,
            "Dataset row count mismatch",
        )
    truth = list(csv.DictReader(io.StringIO(data["data/evaluation_truth.csv"].decode())))
    require(
        len(truth) == 100 and sum(r["is_anomaly"].lower() in ("1", "true") for r in truth) == 5,
        "Evaluation truth/anomaly count mismatch",
    )
    for local in (
        "reports/data_quality.json",
        "reports/evaluation.json",
        "reports/run_manifest.json",
        "models/model_metadata.json",
    ):
        print(local + ":\n" + data[local].decode())
    print(
        "PASS: one successful task; 12 artifact generation/hash/size checks; "
        "256/100 rows; 5 anomalies."
    )
    print(
        "REVIEW REQUIRED: both quality reports passing and saved-model reload verification "
        "in the printed reports. Record metrics as-is."
    )


def main():
    require(
        len(sys.argv) == 2 and sys.argv[1] in ("check", "execute", "verify"),
        "Usage: python3 stage3.py check|execute|verify",
    )
    c = Context()
    reviewed_environment(c)
    digest = approved_image(c)
    job = api("GET", job_url(c))
    validate_job(c, job, digest)
    found = executions(c)
    print("EXISTING EXECUTIONS:", json.dumps(found, indent=2))
    if sys.argv[1] == "check":
        return
    if sys.argv[1] == "execute":
        require(
            not found and job.get("executionCount", 0) == 0,
            "Job has execution history. No second execution is authorized; use verify",
        )
        # A pre-existing reservation, even without completion, blocks accidental namespace reuse.
        from urllib.parse import quote

        reservation = f"{PREFIX}/run-manifests/{RUN_ID}/request.json"
        require(
            api(
                "GET",
                f"https://storage.googleapis.com/storage/v1/b/{c.data}/o/"
                + quote(reservation, safe=""),
                missing=True,
            )
            is None,
            "Run namespace already reserved; stop, do not execute another run",
        )
        require(
            input("After separate Stage 3 approval and policy/quota review, type EXECUTE-ONCE: ")
            == "EXECUTE-ONCE",
            "Approval acknowledgement missing",
        )
        require(not executions(c), "Concurrent execution found; stop")
        once_marker(c, "execute-" + RUN_ID)
        print(gc("run", "jobs", "execute", c.job, "--region=" + REGION, "--wait", timeout=900))
        found = executions(c)
    require(len(found) == 1, "Need exactly one execution; stop for investigation")
    e = api("GET", "https://run.googleapis.com/v2/" + found[0]["name"])
    print(json.dumps(e, indent=2))
    verify(c, e, digest)
    execution_name = e["name"].split("/")[-1]
    query = (
        f'resource.type="cloud_run_job" AND resource.labels.job_name="{c.job}" '
        f'AND labels."run.googleapis.com/execution_name"="{execution_name}"'
    )
    print(gc("logging", "read", query, "--limit=100", json_output=True))
    print("STAGE 3 VERIFICATION FINISHED. No idempotency rerun is included.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, KeyError, subprocess.SubprocessError, OSError) as e:
        sys.exit("STOP: " + str(e))
