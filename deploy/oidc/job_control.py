"""Manual workflow operations using a short-lived impersonated token in memory.

No SDK, Cloud Build, keys, receipt files, IAM writes, job updates or deletions.
Execution returns the asynchronous operation, never reports workload completion.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

from settings import (
    IMAGE_BASE,
    JOB,
    OWNER_ID,
    PROJECT,
    REGION,
    REPO_ID,
    RUNTIME_SA,
    image_uri,
    job_environment,
    job_name,
    job_url,
    load_config,
)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Unexpected Google API redirect; credentials are not forwarded")


class Api:
    def __init__(self, token):
        if not token or any(c.isspace() for c in token):
            raise RuntimeError("Short-lived access token is unavailable")
        self._token = token

    def request(self, method, url, body=None, missing=False):
        if method not in {"GET", "POST"} or not url.startswith(
            ("https://run.googleapis.com/v2/", "https://artifactregistry.googleapis.com/v1/")
        ):
            raise RuntimeError("Operation outside runtime allowlist")
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Authorization": "Bearer " + self._token, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                detail = json.loads(error.read()).get("error", {})
            except (ValueError, AttributeError):
                detail = {}
            if error.code == 404 and detail.get("status") == "NOT_FOUND" and missing:
                return None
            raise RuntimeError(
                f"Google API HTTP {error.code}; stop, do not retry a write"
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise RuntimeError(
                "API outcome uncertain. Inspect Cloud Run before any new approved dispatch"
            ) from None


def verify_dispatch(env, action, config):
    required = {
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY_ID": REPO_ID,
        "GITHUB_REPOSITORY_OWNER_ID": OWNER_ID,
    }
    if any(env.get(k) != v for k, v in required.items()):
        raise RuntimeError(
            "Only manual dispatch from the exact repository owner and main branch is allowed"
        )
    if env.get("CONFIRM_DIGEST") != config["image_digest"] or env.get("CONFIRM_ACTION") != "true":
        raise RuntimeError("Explicit action and exact digest confirmation required")
    if action == "execute" and not env.get("CONFIRM_JOB_UID"):
        raise RuntimeError("Reviewed deployed job UID is required for execution")


def validate_job(job, config, ready=True):
    if job.get("name") != job_name():
        raise RuntimeError("Job name differs from reviewed target")
    if job.get("labels", {}).get("deployment") != "synthetic-quality-phase1":
        raise RuntimeError("Existing job is not marked for this deployment; do not adopt it")
    execution = job.get("template", {})
    task = execution.get("template", {})
    containers = task.get("containers", [])
    if execution.get("taskCount") != 1 or execution.get("parallelism") != 1:
        raise RuntimeError("Task count/parallelism must equal one")
    if (
        task.get("serviceAccount") != RUNTIME_SA
        or task.get("timeout") != "600s"
        or task.get("maxRetries", 0) != 0
    ):
        raise RuntimeError("Runtime identity, timeout or retries mismatch")
    if task.get("vpcAccess") or task.get("volumes") or task.get("encryptionKey"):
        raise RuntimeError("Unexpected network, volumes or encryption; stop for review")
    if len(containers) != 1:
        raise RuntimeError("Exactly one container required")
    c = containers[0]
    if c.get("image") != image_uri(config) or c.get("resources", {}).get("limits") != {
        "cpu": "1",
        "memory": "1Gi",
    }:
        raise RuntimeError("Immutable image or compute sizing mismatch")
    if c.get("command") or c.get("args") or c.get("volumeMounts"):
        raise RuntimeError("Unexpected container entrypoint/args/mounts")
    entries = c.get("env", [])
    if len(entries) != len(job_environment(config)) or any(
        set(x) - {"name", "value"} for x in entries
    ):
        raise RuntimeError("Unexpected environment or secret reference")
    if {x.get("name"): x.get("value") for x in entries} != job_environment(config):
        raise RuntimeError("Job environment differs from approved synthetic smoke test")
    if ready and (
        job.get("reconciling")
        or job.get("terminalCondition", {}).get("state") != "CONDITION_SUCCEEDED"
    ):
        raise RuntimeError("Job is not confirmed ready")
    if not job.get("uid") or not job.get("etag"):
        raise RuntimeError("Job UID/etag missing")


def verify_image(client, config):
    image = quote("synthetic-quality@" + config["image_digest"], safe="")
    url = (
        f"https://artifactregistry.googleapis.com/v1/projects/{PROJECT}/locations/{REGION}/"
        f"repositories/synthetic-quality-phase1/dockerImages/{image}"
    )
    record = client.request("GET", url)
    if record.get("uri") != IMAGE_BASE + "@" + config["image_digest"]:
        raise RuntimeError("Artifact Registry does not match the reviewed immutable image")


def deployment_body(config):
    return {
        "labels": {"deployment": "synthetic-quality-phase1"},
        "template": {
            "taskCount": 1,
            "parallelism": 1,
            "template": {
                "serviceAccount": RUNTIME_SA,
                "timeout": "600s",
                "maxRetries": 0,
                "containers": [
                    {
                        "image": image_uri(config),
                        "resources": {"limits": {"cpu": "1", "memory": "1Gi"}},
                        "env": [
                            {"name": k, "value": v} for k, v in job_environment(config).items()
                        ],
                    }
                ],
            },
        },
    }


def deploy(client, config, sleeper=time.sleep):
    verify_image(client, config)
    job = client.request("GET", job_url(), missing=True)
    if job is not None:
        validate_job(job, config)
        if job.get("executionCount", 0):
            raise RuntimeError("Existing job has execution history; no changes made")
        return {
            "status": "existing job verified; unchanged",
            "job": job_name(),
            "uid": job["uid"],
            "image": image_uri(config),
        }
    # No automatic POST retry. A lost acknowledgement requires read-only inspection.
    operation = client.request(
        "POST",
        f"https://run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/jobs?jobId={JOB}",
        deployment_body(config),
    )
    for _ in range(40):
        job = client.request("GET", job_url(), missing=True)
        if (
            job is not None
            and not job.get("reconciling")
            and job.get("terminalCondition", {}).get("state") == "CONDITION_SUCCEEDED"
        ):
            validate_job(job, config)
            return {
                "status": "job deployed; NOT executed",
                "job": job_name(),
                "uid": job["uid"],
                "image": image_uri(config),
                "operation": operation.get("name"),
            }
        if job is not None and job.get("terminalCondition", {}).get("state") == "CONDITION_FAILED":
            raise RuntimeError("Job reconciliation failed; inspect without creating another job")
        sleeper(3)
    raise RuntimeError(
        "Deployment not confirmed within the short credential window; inspect, do not recreate"
    )


def execute(client, config, expected_uid):
    job = client.request("GET", job_url())
    validate_job(job, config)
    if job["uid"] != expected_uid:
        raise RuntimeError("Job UID differs from reviewed deployment")
    if job.get("executionCount", 0):
        raise RuntimeError("Execution history already exists; no additional execution authorized")
    page = client.request("GET", job_url() + "/executions?pageSize=1")
    if page.get("executions") or page.get("nextPageToken"):
        raise RuntimeError("Execution inventory is not empty; stop")
    # No overrides and no retry. etag rejects a concurrent job configuration change.
    operation = client.request("POST", job_url() + ":run", {"etag": job["etag"]})
    return {
        "status": "execution REQUESTED; completion not verified",
        "job": job_name(),
        "uid": job["uid"],
        "image": image_uri(config),
        "operation": operation.get("name"),
        "execution": operation.get("metadata", {}).get("name"),
    }


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in {"deploy", "execute"}:
        raise RuntimeError("Usage: job_control.py deploy|execute")
    action = sys.argv[1]
    config = load_config(require_enabled=True)
    verify_dispatch(os.environ, action, config)
    client = Api(os.environ.get("GCP_ACCESS_TOKEN", ""))
    result = (
        deploy(client, config)
        if action == "deploy"
        else execute(client, config, os.environ["CONFIRM_JOB_UID"])
    )
    rendered = json.dumps(result, indent=2)
    print(rendered)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
            summary.write("```json\n" + rendered + "\n```\n")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError) as error:
        sys.exit("STOP: " + str(error))
