#!/usr/bin/env python3
"""Read-only Cloud Shell preflight. Importing this module performs no I/O.

Uses the active gcloud user credential in memory, never writes tokens or keys,
and never enables APIs, repairs IAM, or creates service identities.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

PROJECT = "gcp-intelligent-data-quality"
REGION = "asia-south1"
REV = "34cc3d46b662a9f0560c3ff846d68b327d52e4b8"
REPOSITORY = "synthetic-quality-phase1"
JOB = "synthetic-quality-phase1-smoke"
PREFIX = "synthetic-quality"
RUN_ID = "smoke-001"
_ACCOUNT: str | None = None

REQUIRED_APIS = {
    "serviceusage.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "iam.googleapis.com",
    "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com",
    "run.googleapis.com",
    "storage.googleapis.com",
    "logging.googleapis.com",
    "orgpolicy.googleapis.com",
}

# Unsupported identifiers are reported, not mistaken for unset policies. The
# actual applicable list is also inspected for custom and service constraints.
POLICY_TARGETS = {
    "gcp.resourceLocations",
    "gcp.restrictNonCmekServices",
    "gcp.restrictCmekCryptoKeyProjects",
    "gcp.restrictEndpointUsage",
    "iam.allowedPolicyMemberDomains",
    "iam.managed.allowedPolicyMembers",
    "iam.automaticIamGrantsForDefaultServiceAccounts",
    "iam.disableServiceAccountCreation",
    "iam.disableServiceAccountKeyCreation",
    "iam.disableServiceAccountKeyUpload",
    "iam.disableCrossProjectServiceAccountUsage",
    "storage.uniformBucketLevelAccess",
    "storage.publicAccessPrevention",
    "storage.softDeletePolicySeconds",
    "storage.retentionPolicySeconds",
    "run.allowedIngress",
    "run.allowedVPCEgress",
    "run.allowedBinaryAuthorizationPolicies",
    "run.requireVPCConnector",
    "cloudbuild.allowedWorkerPools",
    "cloudbuild.allowedIntegrations",
    "cloudbuild.disableCreateDefaultServiceAccount",
    "cloudbuild.useBuildServiceAccount",
    "cloudbuild.useComputeServiceAccount",
    "serviceuser.services",
}


class Stop(RuntimeError):
    """A prerequisite is missing or cannot be verified safely."""


def gc(*args: str, json_output: bool = False, timeout: int = 120):
    """Run gcloud without interactive API-enablement or other prompts.

    Only the caller decides which gcloud command to invoke. All calls made by
    this module are read-only. Explicit project and account avoid default drift.
    """
    command = ["gcloud", "--quiet", f"--project={PROJECT}"]
    if _ACCOUNT:
        command.append(f"--account={_ACCOUNT}")
    command.extend(args)
    if json_output:
        command.append("--format=json")
    environment = os.environ.copy()
    environment["CLOUDSDK_CORE_DISABLE_PROMPTS"] = "1"
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            env=environment,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Stop(f"gcloud unavailable or timed out: {type(error).__name__}") from None
    if result.returncode:
        # Never echo a command or stdout: an auth command could contain tokens.
        raise Stop(f"gcloud {' '.join(args[:2])} failed: {result.stderr.strip()}")
    if json_output:
        try:
            return json.loads(result.stdout)
        except ValueError:
            raise Stop("gcloud returned invalid JSON; inspection is inconclusive") from None
    return result.stdout.strip()


def api(method: str, url: str, body=None, missing: bool = False):
    """Use active-user auth for one official API call; classify structured errors.

    Return None ONLY for an explicitly allowed HTTP 404 / NOT_FOUND response.
    A missing resource is never inferred from authentication, API, quota, or
    transport failures. Response/credential data is never written to disk.
    """
    parsed = urllib.parse.urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not parsed.hostname.endswith(".googleapis.com")
        or parsed.username
        or parsed.password
    ):
        raise Stop("Only HTTPS Google API endpoints are allowed")
    token = gc("auth", "print-access-token")
    if not token or any(character.isspace() for character in token):
        raise Stop("Could not obtain a valid active-user access token")
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Goog-User-Project": PROJECT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = response.read()
        return json.loads(result) if result else {}
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read()).get("error", {})
        except (ValueError, AttributeError):
            detail = {}
        reasons = {
            item.get("reason") for item in detail.get("details", []) if isinstance(item, dict)
        }
        status = detail.get("status", "UNCLASSIFIED")
        if "SERVICE_DISABLED" in reasons:
            raise Stop(
                f"API DISABLED at {parsed.hostname}; enablement requires separate approval"
            ) from None
        if error.code == 401:
            raise Stop(f"AUTHENTICATION FAILED at {parsed.hostname}") from None
        if error.code == 403:
            raise Stop(f"PERMISSION/ACCESS DENIED at {parsed.hostname}; do not bypass it") from None
        storage_not_found = (
            parsed.hostname == "storage.googleapis.com"
            and detail.get("code") == 404
            and any(
                item.get("reason") == "notFound"
                for item in detail.get("errors", [])
                if isinstance(item, dict)
            )
        )
        if error.code == 404 and (status == "NOT_FOUND" or storage_not_found) and missing:
            return None
        raise Stop(
            f"API HTTP {error.code} / {status} at {parsed.hostname}; inspection incomplete"
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise Stop(
            f"Transport or response failure at {parsed.hostname}; inspection incomplete"
        ) from None


def get(url: str, missing: bool = False):
    return api("GET", url, missing=missing)


def require(condition: bool, message: str):
    if not condition:
        raise Stop(message)


def pages(url: str, field: str):
    """Read every API list page; do not infer absence from the first page."""
    items = []
    token = None
    seen = set()
    while True:
        page_url = url
        if token:
            page_url += ("&" if "?" in url else "?") + urllib.parse.urlencode({"pageToken": token})
        result = get(page_url)
        require(isinstance(result, dict), "Unexpected list response")
        items.extend(result.get(field, []))
        token = result.get("nextPageToken")
        if not token:
            return items
        require(token not in seen, "Repeated page token; inventory is incomplete")
        seen.add(token)


class Context:
    """Validate auth, project, APIs, existing SAs, actAs, and service agents."""

    def __init__(self):
        global _ACCOUNT
        _ACCOUNT = None
        for selector in (
            "auth/impersonate_service_account",
            "auth/access_token_file",
            "auth/credential_file_override",
        ):
            value = gc("config", "get-value", selector)
            require(
                value in ("", "(unset)", "None"), f"Unexpected gcloud selector {selector}; stop"
            )
        require(
            not os.environ.get("CLOUDSDK_AUTH_ACCESS_TOKEN"),
            "Access-token environment override configured; stop",
        )
        active = gc("auth", "list", "--filter=status:ACTIVE", json_output=True)
        require(
            len(active) == 1 and bool(active[0].get("account")),
            "Exactly one active operator is required",
        )
        self.account = active[0]["account"]
        require(
            not self.account.endswith(".gserviceaccount.com"),
            "Expected a signed-in Cloud Shell user, not an impersonated/service identity",
        )
        _ACCOUNT = self.account
        project = gc("projects", "describe", PROJECT, json_output=True)
        require(
            project.get("projectId") == PROJECT and project.get("lifecycleState") == "ACTIVE",
            "Wrong or inactive project",
        )
        try:
            remote = subprocess.run(
                [
                    "git",
                    "ls-remote",
                    "https://github.com/chandrakanth02512-ctrl/ml-synthetic-data-quality.git",
                    "refs/heads/main",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            ).stdout.split()
        except (OSError, subprocess.SubprocessError):
            raise Stop("Cannot verify current GitHub main; source inspection incomplete") from None
        require(
            bool(remote) and remote[0] == REV,
            "GitHub main differs from reviewed revision; stop for source review",
        )
        self.number = str(project.get("projectNumber", ""))
        require(self.number.isdigit(), "Project number unavailable")
        billing = gc("billing", "projects", "describe", PROJECT, json_output=True)
        require(
            billing.get("billingEnabled") is True and bool(billing.get("billingAccountName")),
            "Active linked billing not confirmed",
        )
        enabled = gc("services", "list", "--enabled", json_output=True)
        names = {
            item.get("config", {}).get("name", item.get("name", "").split("/")[-1])
            for item in enabled
        }
        missing = sorted(REQUIRED_APIS - names)
        require(
            not missing,
            f"Required inspection/workload APIs disabled or unconfirmed: {', '.join(missing)}. "
            "No API was enabled.",
        )
        self.data = f"synthetic-quality-{self.number}-data-as1"
        self.source = f"synthetic-quality-{self.number}-source-as1"
        self.repository = REPOSITORY
        self.job = JOB
        self.runtime = f"synthetic-quality-runtime@{PROJECT}.iam.gserviceaccount.com"
        self.build = f"synthetic-quality-build@{PROJECT}.iam.gserviceaccount.com"
        self.role = f"projects/{PROJECT}/roles/syntheticQualityObjects"
        self.build_agent = f"service-{self.number}@gcp-sa-cloudbuild.iam.gserviceaccount.com"
        self.run_agent = f"service-{self.number}@serverless-robot-prod.iam.gserviceaccount.com"
        for account in (self.runtime, self.build):
            path = f"https://iam.googleapis.com/v1/projects/{PROJECT}/serviceAccounts/{account}"
            sa = get(path)
            require(
                sa.get("projectId") == PROJECT
                and sa.get("email") == account
                and not sa.get("disabled", False),
                f"Missing, disabled, or wrong-project service account: {account}",
            )
            result = api(
                "POST", path + ":testIamPermissions", {"permissions": ["iam.serviceAccounts.actAs"]}
            )
            require(
                "iam.serviceAccounts.actAs" in result.get("permissions", []),
                f"Operator {self.account} lacks effective iam.serviceAccounts.actAs on {account}",
            )
            print(f"PASS: operator actAs on {account}")
        self.project_policy = gc("projects", "get-iam-policy", PROJECT, json_output=True)
        for account, role in (
            (self.build_agent, "roles/cloudbuild.serviceAgent"),
            (self.run_agent, "roles/run.serviceAgent"),
        ):
            member = f"serviceAccount:{account}"
            require(
                any(
                    binding.get("role") == role
                    and not binding.get("condition")
                    and member in binding.get("members", [])
                    for binding in self.project_policy.get("bindings", [])
                ),
                f"Service agent unconditional {role} binding missing/unverified: {account}. "
                "Stop for targeted repair review.",
            )
            print(f"PASS: {account} has unconditional {role}")
        print(f"PASS: basic read-only preflight for {PROJECT} as {self.account}")

    def check_policies(self):
        base = f"https://orgpolicy.googleapis.com/v2/projects/{self.number}"
        constraints = pages(base + "/constraints", "constraints")
        known = {item["name"].split("/")[-1]: item for item in constraints if item.get("name")}
        require(bool(known), "Applicable constraint list is empty; cannot classify policies safely")
        targets = POLICY_TARGETS | {
            name
            for name in known
            if name.startswith(
                ("custom.", "run.", "cloudbuild.", "storage.", "artifactregistry.", "iam.managed.")
            )
        }
        report = []
        for name in sorted(targets):
            if name not in known:
                report.append(
                    {
                        "constraint": name,
                        "state": "UNSUPPORTED_OR_NOT_APPLICABLE",
                        "action": (
                            "Administrator must review any safety requirement "
                            "represented by this identifier"
                        ),
                    }
                )
                continue
            path = base + "/policies/" + urllib.parse.quote(name, safe=".")
            local = get(path, missing=True)
            effective = get(path + ":getEffectivePolicy")
            require(
                isinstance(effective, dict) and bool(effective),
                f"No usable effective policy for {name}",
            )
            report.append(
                {
                    "constraint": name,
                    "state": "LOCALLY_UNSET" if local is None else "LOCALLY_SET",
                    "constraint_definition": known[name],
                    "effective_policy": effective,
                }
            )
        print(json.dumps({"project": PROJECT, "policies": report}, indent=2, sort_keys=True))
        print(
            "STOP FOR REVIEW: effective policies, all applicable ancestor/custom restrictions, "
            "quotas, VPC Service Controls and deny policies must be reviewed before provisioning. "
            "This report does not automatically approve compatibility."
        )
        return report


def main():
    try:
        context = Context()
        context.check_policies()
    except Stop as error:
        print(f"STOP: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
