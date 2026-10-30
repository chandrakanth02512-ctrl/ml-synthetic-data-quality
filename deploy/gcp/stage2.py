"""One manual build and create-only job deployment; independent Cloud Shell session."""

import json
import subprocess
import sys
from pathlib import Path

from preflight import PREFIX, PROJECT, REGION, REV, RUN_ID, gc
from stage1 import iam, inspect, require

IMAGE = "synthetic-quality"
REPO_URL = "https://github.com/chandrakanth02512-ctrl/ml-synthetic-data-quality.git"


def job_url(c):
    return f"https://run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/jobs/{c.job}"


def image_uri(c):
    return f"{REGION}-docker.pkg.dev/{PROJECT}/{c.repository}/{IMAGE}"


def env_vars(c):
    return {
        "GCP_PROJECT_ID": PROJECT,
        "GCS_BUCKET": c.data,
        "GCS_PREFIX": PREFIX,
        "RUN_ID": RUN_ID,
        "TRAINING_ROWS": "256",
        "EVALUATION_ROWS": "100",
        "TRAINING_SEED": "42",
        "EVALUATION_SEED": "123",
    }


def reviewed_environment(c):
    c.check_policies()
    inspect(c, complete=True)
    iam(c, require_bindings=True)


def builds(c):
    # No --limit: inspect complete regional inventory; a failed listing always stops.
    all_builds = gc("builds", "list", "--region=" + REGION, json_output=True)
    return [
        b
        for b in all_builds
        if b.get("substitutions", {}).get("_REPOSITORY") == c.repository
        or any(image_uri(c) in x.get("name", "") for x in b.get("results", {}).get("images", []))
    ]


def approved_image(c):
    from stage2_recovery import recovery_image

    return recovery_image(c)


def validate_job(c, j, digest):
    require(
        j.get("name") == f"projects/{PROJECT}/locations/{REGION}/jobs/{c.job}", "Job name mismatch"
    )
    require(
        j.get("labels", {}).get("deployment") == "synthetic-quality-phase1",
        "Unmarked existing job requires ownership review",
    )
    t = j.get("template", {})
    require(t.get("taskCount") == 1 and t.get("parallelism") == 1, "Job count/parallelism mismatch")
    task = t.get("template", {})
    require(
        task.get("serviceAccount") == c.runtime
        and task.get("timeout") == "600s"
        and task.get("maxRetries", 0) == 0,
        "Job identity/timeout/retries mismatch",
    )
    require(
        not task.get("vpcAccess") and not task.get("volumes") and not task.get("encryptionKey"),
        "Unexpected network, volumes or encryption; review",
    )
    containers = task.get("containers", [])
    require(len(containers) == 1, "Expected one container")
    container = containers[0]
    require(container.get("image") == image_uri(c) + "@" + digest, "Job image digest mismatch")
    require(
        not container.get("command")
        and not container.get("args")
        and not container.get("volumeMounts"),
        "Unexpected entrypoint/args/mounts",
    )
    require(
        container.get("resources", {}).get("limits") == {"cpu": "1", "memory": "1Gi"},
        "Job CPU/memory mismatch",
    )
    env = container.get("env", [])
    require(
        len(env) == len(env_vars(c)) and all(set(x) <= {"name", "value"} for x in env),
        "Unexpected env/secrets",
    )
    require({x.get("name"): x.get("value") for x in env} == env_vars(c), "Job environment mismatch")
    require(
        not j.get("reconciling")
        and j.get("terminalCondition", {}).get("state") == "CONDITION_SUCCEEDED",
        "Job not confirmed ready; inspect before proceeding",
    )
    print("VERIFIED JOB:", c.job, "1 task / 1 CPU / 1Gi / 600s / 0 retries / 256+100 rows")


def git(*args, cwd=None):
    r = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return r.stdout.strip()


def once_marker(c, action):
    # A durable, non-secret receipt complements server-side inventory across sessions.
    path = Path.home() / ".local/state/synthetic-quality-phase1"
    path.mkdir(parents=True, exist_ok=True)
    receipt = path / (PROJECT + "-" + action + "-" + REV + ".attempt")
    try:
        with receipt.open("x") as f:
            json.dump({"project": PROJECT, "revision": REV, "action": action}, f)
    except FileExistsError:
        raise RuntimeError(
            "Prior attempt marker exists. Inspect server history; do not repeat: " + str(receipt)
        ) from None
    return receipt


def main():
    from stage2_recovery import main as recovery_main

    recovery_main()


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, subprocess.SubprocessError, OSError) as e:
        sys.exit("STOP: " + str(e))
