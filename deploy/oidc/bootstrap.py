"""Owner-reviewed, idempotent WIF setup. No I/O on import; plan is offline.

check reads only. apply requires an exact approval phrase and an enabled, verified
image pin. No keys, builds, workloads, resource deletion or broad executor grant.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

from job_control import NoRedirect, validate_job
from settings import (
    APP_REV,
    DEPLOY_SA,
    EXECUTE_SA,
    JOB,
    OWNER_ID,
    PROJECT,
    PROJECT_NUMBER,
    REGION,
    REPO,
    REPO_ID,
    REPOSITORY,
    RUNTIME_SA,
    load_config,
)

APPROVAL = "WIF-SETUP-984592102391"
ISSUER = "https://token.actions.githubusercontent.com"
ENABLE_APIS = {"iamcredentials.googleapis.com", "sts.googleapis.com"}
REQUIRED_APIS = {
    "iam.googleapis.com",
    "serviceusage.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "run.googleapis.com",
}
OWNER_ACCOUNT = None


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def identity_plan():
    pools, bindings = [], []
    roles = [
        {
            "id": "syntheticQualityJobDeployer",
            "name": f"projects/{PROJECT}/roles/syntheticQualityJobDeployer",
            "title": "Synthetic quality create-only deployer",
            "scope": "project",
            "permissions": ["run.jobs.create", "run.jobs.get"],
        },
        {
            "id": "syntheticQualityJobExecutor",
            "name": f"projects/{PROJECT}/roles/syntheticQualityJobExecutor",
            "title": "Synthetic quality exact-job executor",
            "scope": "job",
            "permissions": [
                "run.jobs.run",
                "run.jobs.get",
                "run.executions.get",
                "run.executions.list",
            ],
        },
    ]
    for action, service_account in (("deploy", DEPLOY_SA), ("execute", EXECUTE_SA)):
        pool_id = "synthetic-quality-" + action
        pool = f"projects/{PROJECT_NUMBER}/locations/global/workloadIdentityPools/{pool_id}"
        provider = pool + "/providers/github"
        mapping = {
            "google.subject": "assertion.sub",
            "attribute.repository_id": "assertion.repository_id",
            "attribute.repository_owner_id": "assertion.repository_owner_id",
            "attribute.ref": "assertion.ref",
            "attribute.ref_type": "assertion.ref_type",
            "attribute.event_name": "assertion.event_name",
            "attribute.workflow_ref": "assertion.workflow_ref",
            "attribute.environment": "assertion.environment",
            "attribute.actor_id": "assertion.actor_id",
        }
        condition = " && ".join(
            [
                f"attribute.repository_id == '{REPO_ID}'",
                f"attribute.repository_owner_id == '{OWNER_ID}'",
                f"attribute.actor_id == '{OWNER_ID}'",
                "attribute.ref == 'refs/heads/main'",
                "attribute.ref_type == 'branch'",
                "attribute.event_name == 'workflow_dispatch'",
                f"attribute.workflow_ref == '{REPO}/.github/workflows/"
                f"gcp-{action}.yml@refs/heads/main'",
                f"attribute.environment == 'gcp-{action}'",
            ]
        )
        pools.append(
            {
                "id": pool_id,
                "name": pool,
                "service_account": service_account,
                "provider": {
                    "id": "github",
                    "name": provider,
                    "issuerUri": ISSUER,
                    "allowedAudiences": ["https://iam.googleapis.com/" + provider],
                    "attributeMapping": mapping,
                    "attributeCondition": condition,
                },
            }
        )
        bindings.append(
            {
                "scope": "service_account",
                "resource": service_account,
                "member": (
                    f"principalSet://iam.googleapis.com/{pool}/attribute.repository_id/{REPO_ID}"
                ),
                "role": "roles/iam.workloadIdentityUser",
            }
        )
    bindings.extend(
        [
            {
                "scope": "project",
                "resource": PROJECT,
                "member": "serviceAccount:" + DEPLOY_SA,
                "role": roles[0]["name"],
            },
            {
                "scope": "repository",
                "resource": REPOSITORY,
                "member": "serviceAccount:" + DEPLOY_SA,
                "role": "roles/artifactregistry.reader",
            },
            {
                "scope": "service_account",
                "resource": RUNTIME_SA,
                "member": "serviceAccount:" + DEPLOY_SA,
                "role": "roles/iam.serviceAccountUser",
            },
            {
                "scope": "job",
                "resource": JOB,
                "member": "serviceAccount:" + EXECUTE_SA,
                "role": roles[1]["name"],
            },
        ]
    )
    return {
        "project": PROJECT,
        "project_number": str(PROJECT_NUMBER),
        "region": REGION,
        "pools": pools,
        "roles": roles,
        "bindings": bindings,
        "required_apis": sorted(REQUIRED_APIS),
        "enable_apis": sorted(ENABLE_APIS),
        "deferred_rule": (
            "If job is absent, owner reruns this procedure after approved deployment to add "
            "the exact-job executor grant. No project-wide execute or deployer setIamPolicy."
        ),
    }


def gc(*args, json_output=False):
    env = os.environ.copy()
    env["CLOUDSDK_CORE_DISABLE_PROMPTS"] = "1"
    command = ["gcloud", "--quiet", "--project=" + PROJECT]
    if OWNER_ACCOUNT:
        command.append("--account=" + OWNER_ACCOUNT)
    command.extend(args)
    if json_output:
        command.append("--format=json")
    try:
        result = subprocess.run(
            command, check=False, text=True, capture_output=True, timeout=180, env=env
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("gcloud unavailable or timed out; no retry") from error
    require(result.returncode == 0, "gcloud inspection/setup failed: " + result.stderr.strip())
    return json.loads(result.stdout) if json_output else result.stdout.strip()


def api_get(url, missing=False):
    parsed = urllib.parse.urlparse(url)
    require(
        parsed.scheme == "https"
        and parsed.hostname
        in {"iam.googleapis.com", "run.googleapis.com", "artifactregistry.googleapis.com"},
        "Unexpected API endpoint",
    )
    token = gc("auth", "print-access-token")
    request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read()).get("error", {})
        except ValueError:
            detail = {}
        reasons = {x.get("reason") for x in detail.get("details", []) if isinstance(x, dict)}
        if "SERVICE_DISABLED" in reasons:
            raise RuntimeError("Inspection API disabled; stop for approval") from None
        if error.code == 404 and detail.get("status") == "NOT_FOUND" and missing:
            return None
        raise RuntimeError(
            f"HTTP {error.code} / {detail.get('status', 'UNKNOWN')}: inspection/setup blocked"
        ) from None
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise RuntimeError("API transport/response failure; do not treat as missing") from error


def pages(url, field):
    values, tokens, base, token = [], set(), url, None
    while True:
        page_url = base
        if token:
            page_url += ("&" if "?" in base else "?") + urllib.parse.urlencode({"pageToken": token})
        result = api_get(page_url)
        values.extend(result.get(field, []))
        token = result.get("nextPageToken")
        if not token:
            return values
        require(token not in tokens, "Repeated page token; incomplete inventory")
        tokens.add(token)


def validate_resource(kind, actual, expected):
    require(actual is not None, kind + " is missing")
    if kind == "role":
        require(
            actual.get("name") == expected["name"]
            and not actual.get("deleted")
            and actual.get("stage") == "GA"
            and set(actual.get("includedPermissions", [])) == set(expected["permissions"]),
            "Existing custom role differs; stop without repair",
        )
    elif kind == "service_account":
        require(
            actual.get("email") == expected["email"]
            and actual.get("projectId") == PROJECT
            and not actual.get("disabled", False),
            "Existing service account differs/disabled",
        )
    elif kind == "pool":
        require(
            actual.get("name") == expected["name"]
            and actual.get("state") == "ACTIVE"
            and not actual.get("disabled", False),
            "Existing pool differs/disabled/deleted",
        )
    elif kind == "provider":
        require(
            actual.get("name") == expected["name"]
            and actual.get("state") == "ACTIVE"
            and not actual.get("disabled", False),
            "Existing provider differs/disabled/deleted",
        )
        require(
            actual.get("attributeMapping") == expected["attributeMapping"]
            and actual.get("attributeCondition") == expected["attributeCondition"]
            and actual.get("oidc", {}).get("issuerUri") == expected["issuerUri"]
            and set(actual.get("oidc", {}).get("allowedAudiences", []))
            == set(expected["allowedAudiences"]),
            "Existing provider trust/audience differs; stop without broadening",
        )
    else:
        raise RuntimeError("Unknown resource validation kind")


def binding_present(policy, binding):
    for entry in policy.get("bindings", []):
        if entry.get("role") == binding["role"] and binding["member"] in entry.get("members", []):
            require(
                not entry.get("condition"),
                "Planned grant exists conditionally; do not broaden it automatically",
            )
            return True
    return False


def policy_command(binding, add=False):
    action = "add-iam-policy-binding" if add else "get-iam-policy"
    scope, resource = binding["scope"], binding["resource"]
    if scope == "project":
        command = ["projects", action, resource]
    elif scope == "repository":
        command = ["artifacts", "repositories", action, resource, "--location=" + REGION]
    elif scope == "service_account":
        command = ["iam", "service-accounts", action, resource]
    else:
        command = ["run", "jobs", action, resource, "--region=" + REGION]
    if add:
        command += [
            "--member=" + binding["member"],
            "--role=" + binding["role"],
            "--condition=None",
        ]
    return command


def owner_context():
    global OWNER_ACCOUNT
    OWNER_ACCOUNT = None
    for setting in (
        "auth/impersonate_service_account",
        "auth/access_token_file",
        "auth/credential_file_override",
    ):
        require(
            gc("config", "get-value", setting) in ("", "(unset)", "None"),
            "Unexpected auth override; stop",
        )
    require(
        not os.environ.get("CLOUDSDK_AUTH_ACCESS_TOKEN"),
        "Expected signed-in Cloud Shell owner, not token override",
    )
    active = gc("auth", "list", "--filter=status:ACTIVE", json_output=True)
    require(
        len(active) == 1 and not active[0]["account"].endswith(".gserviceaccount.com"),
        "Expected one active human Cloud Shell owner",
    )
    OWNER_ACCOUNT = active[0]["account"]
    project = gc("projects", "describe", PROJECT, json_output=True)
    require(
        project.get("projectId") == PROJECT
        and str(project.get("projectNumber")) == str(PROJECT_NUMBER)
        and project.get("lifecycleState") == "ACTIVE",
        "Wrong or inactive project",
    )
    enabled = gc("services", "list", "--enabled", json_output=True)
    names = {x.get("config", {}).get("name", x.get("name", "").rsplit("/", 1)[-1]) for x in enabled}
    require(
        REQUIRED_APIS <= names,
        "Required existing APIs disabled: " + ", ".join(sorted(REQUIRED_APIS - names)),
    )
    return ENABLE_APIS - names


def verify_image(cfg):
    require(
        bool(cfg.get("image_digest")) and bool(cfg.get("replacement_build_id")),
        "Reviewed image pin incomplete; no setup writes allowed",
    )
    require(
        cfg.get("replacement_build_id") != "6f75c5dd-cc3a-4d47-9c9d-43f51fa3c4ba",
        "Cancelled build cannot be an image source",
    )
    require(
        cfg.get("data_bucket") == f"synthetic-quality-{PROJECT_NUMBER}-data-as1",
        "Unexpected/protected data bucket",
    )
    build = gc(
        "builds", "describe", cfg["replacement_build_id"], "--region=" + REGION, json_output=True
    )
    require(
        build.get("projectId") == PROJECT
        and build.get("id") == cfg["replacement_build_id"]
        and build.get("status") == "SUCCESS",
        "Replacement build is not exact successful project build",
    )
    require(
        len(build.get("steps", [])) == 1
        and build["steps"][0].get("name") == "gcr.io/cloud-builders/docker"
        and build["steps"][0].get("status") == "SUCCESS",
        "Replacement Docker step is not successful",
    )
    require(
        build.get("substitutions", {}).get("_TAG") == APP_REV + "-recovery1",
        "Replacement application revision tag differs",
    )
    image = f"{REGION}-docker.pkg.dev/{PROJECT}/{REPOSITORY}/synthetic-quality"
    expected_name = image + ":" + APP_REV + "-recovery1"
    require(
        any(
            x.get("digest") == cfg["image_digest"] and x.get("name") == expected_name
            for x in build.get("results", {}).get("images", [])
        ),
        "Replacement published image digest differs",
    )
    registry = gc(
        "artifacts",
        "docker",
        "images",
        "describe",
        image + "@" + cfg["image_digest"],
        json_output=True,
    )
    require(
        registry.get("image_summary", {}).get("digest") == cfg["image_digest"],
        "Registry immutable digest is not verified",
    )
    print(
        "PASS: existing successful replacement image verified by immutable digest; "
        "no build submitted"
    )


def resource_specs(plan):
    specs = []
    for role in plan["roles"]:
        specs.append(
            (
                "role",
                "https://iam.googleapis.com/v1/" + role["name"],
                role,
                [
                    "iam",
                    "roles",
                    "create",
                    role["id"],
                    "--stage=GA",
                    "--title=" + role["title"],
                    "--permissions=" + ",".join(role["permissions"]),
                ],
            )
        )
    for pool in plan["pools"]:
        account = pool["service_account"]
        specs.append(
            (
                "service_account",
                f"https://iam.googleapis.com/v1/projects/{PROJECT}/serviceAccounts/{account}",
                {"email": account},
                ["iam", "service-accounts", "create", account.split("@")[0]],
            )
        )
        specs.append(
            (
                "pool",
                "https://iam.googleapis.com/v1/" + pool["name"],
                pool,
                ["iam", "workload-identity-pools", "create", pool["id"], "--location=global"],
            )
        )
        provider = pool["provider"]
        specs.append(
            (
                "provider",
                "https://iam.googleapis.com/v1/" + provider["name"],
                provider,
                [
                    "iam",
                    "workload-identity-pools",
                    "providers",
                    "create-oidc",
                    "github",
                    "--location=global",
                    "--workload-identity-pool=" + pool["id"],
                    "--issuer-uri=" + ISSUER,
                    "--allowed-audiences=" + provider["allowedAudiences"][0],
                    "--attribute-mapping="
                    + ",".join(k + "=" + v for k, v in provider["attributeMapping"].items()),
                    "--attribute-condition=" + provider["attributeCondition"],
                ],
            )
        )
    return specs


def inspect(plan, cfg):
    missing, present_accounts = [], {RUNTIME_SA}
    for kind, url, expected, command in resource_specs(plan):
        actual = api_get(url, missing=True)
        if actual is None:
            missing.append((kind, url, expected, command))
            print("MISSING:", kind, expected.get("name", expected.get("email")))
        else:
            validate_resource(kind, actual, expected)
            if kind == "service_account":
                present_accounts.add(expected["email"])
                keys = api_get(url + "/keys?keyTypes=USER_MANAGED")
                require(
                    not keys.get("keys"),
                    "Dedicated federation identity has user-managed keys; "
                    "stop without deleting them",
                )
            if kind == "pool":
                providers = pages(url + "/providers", "workloadIdentityPoolProviders")
                require(
                    all(p["name"] == expected["provider"]["name"] for p in providers),
                    "Unexpected provider in identity pool; principalSet isolation not established",
                )
            print("MATCH:", kind, expected.get("name", expected.get("email")))
    runtime = api_get(
        f"https://iam.googleapis.com/v1/projects/{PROJECT}/serviceAccounts/{RUNTIME_SA}"
    )
    validate_resource("service_account", runtime, {"email": RUNTIME_SA})
    project_policy = gc("projects", "get-iam-policy", PROJECT, json_output=True)
    for grant in project_policy.get("bindings", []):
        for member in grant.get("members", []):
            require(
                not any(
                    member.startswith(scheme + "iam.googleapis.com/" + pool["name"] + "/")
                    for scheme in ("principalSet://", "principal://")
                    for pool in plan["pools"]
                ),
                "Project-level grant to a federation pool bypasses identity isolation; "
                "stop without removing it",
            )
        for account in (DEPLOY_SA, EXECUTE_SA):
            if "serviceAccount:" + account not in grant.get("members", []):
                continue
            allowed = {plan["roles"][0]["name"]} if account == DEPLOY_SA else set()
            require(
                grant.get("role") in allowed and not grant.get("condition"),
                "Existing deployment identity has unexpected project grant; "
                "stop without modifying it",
            )
    for pool in plan["pools"]:
        if pool["service_account"] not in present_accounts:
            continue
        target = next(
            b
            for b in plan["bindings"]
            if b["scope"] == "service_account" and b["resource"] == pool["service_account"]
        )
        policy = gc(*policy_command(target), json_output=True)
        for grant in policy.get("bindings", []):
            require(
                grant.get("role") == "roles/iam.workloadIdentityUser"
                and set(grant.get("members", [])) <= {target["member"]}
                and not grant.get("condition"),
                "Existing dedicated service account has unexpected impersonation trust; "
                "stop without modifying it",
            )
    job = api_get(
        f"https://run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/jobs/{JOB}",
        missing=True,
    )
    if job is not None:
        validate_job(job, cfg)
    else:
        print(
            "DEFERRED: exact job executor binding; job absent. "
            "Owner must rerun after approved deployment."
        )
    pending = []
    for binding in plan["bindings"]:
        if binding["scope"] == "job" and job is None:
            continue
        if binding["scope"] == "service_account" and binding["resource"] not in present_accounts:
            pending.append(binding)
            continue
        policy = gc(*policy_command(binding), json_output=True)
        for grant in policy.get("bindings", []):
            for account in (DEPLOY_SA, EXECUTE_SA):
                if "serviceAccount:" + account not in grant.get("members", []):
                    continue
                allowed = {
                    b["role"]
                    for b in plan["bindings"]
                    if b["scope"] == binding["scope"]
                    and b["resource"] == binding["resource"]
                    and b["member"] == "serviceAccount:" + account
                }
                require(
                    grant.get("role") in allowed and not grant.get("condition"),
                    "Existing identity grant exceeds planned scope; stop without modifying IAM",
                )
        if not binding_present(policy, binding):
            pending.append(binding)
            print("MISSING BINDING:", binding["scope"], binding["member"], binding["role"])
    return missing, pending, job is None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "check", "apply"))
    parser.add_argument("--approve", default="")
    args = parser.parse_args()
    plan = identity_plan()
    if args.action == "plan":
        print(json.dumps(plan, indent=2, sort_keys=True))
        return
    cfg = load_config(require_enabled=args.action == "apply")
    if args.action == "apply":
        require(cfg.get("enabled") is True, "Reviewed configuration disabled/incomplete; no writes")
        require(args.approve == APPROVAL, "Exact owner setup approval phrase required")
    missing_apis = owner_context()
    verify_image(cfg)
    missing, pending, deferred = inspect(plan, cfg)
    if args.action == "check":
        print(
            "READ-ONLY CHECK COMPLETE; no resources/IAM changed. Missing API enablement:",
            sorted(missing_apis),
        )
        return
    # Everything above this point is read-only. Owner approval covers only additions.
    if missing_apis:
        gc("services", "enable", *sorted(missing_apis))
    for kind, url, expected, command in missing:
        current = api_get(url, missing=True)
        if current is None:
            gc(*command)
            current = api_get(url)
        validate_resource(kind, current, expected)
    for binding in pending:
        current = gc(*policy_command(binding), json_output=True)
        if not binding_present(current, binding):
            gc(*policy_command(binding, add=True))
        require(
            binding_present(gc(*policy_command(binding), json_output=True), binding),
            "New binding not visible; stop and inspect propagation",
        )
    print("SETUP ADDITIONS VERIFIED. No keys, builds, deployment or execution performed.")
    if deferred:
        print(
            "EXECUTION AUTHORIZATION PENDING: owner reruns this idempotent setup after "
            "the reviewed job exists; no project-wide executor grant was added."
        )


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError, KeyError) as error:
        sys.exit("STOP: " + str(error))
