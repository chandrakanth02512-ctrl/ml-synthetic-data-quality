"""Read-only, summary-only Cloud Shell authorization preflight.

No cloud mutation path, retries, keys, login, impersonation, downloads or inventory
files. Existing human gcloud/gh authentication is required. Imports perform no I/O.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import quote, urlencode

import bootstrap
import review_read
from job_control import validate_job
from settings import (
    APP_REV,
    DEPLOY_SA,
    EXECUTE_SA,
    IMAGE,
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
from verify_source_archive import expected_sources, validate_evidence

CATEGORIES = (
    "context",
    "iam",
    "organization_policy",
    "deny_pab",
    "vpc_sc",
    "apis_quotas",
    "artifact_registry",
    "github",
    "wif",
)
STATUSES = {"PASS", "EXPECTED MISSING", "BLOCKED", "UNVERIFIED"}
BUILD_SA = f"synthetic-quality-build@{PROJECT}.iam.gserviceaccount.com"
DATA_BUCKET = f"synthetic-quality-{PROJECT_NUMBER}-data-as1"
SOURCE_BUCKET = f"synthetic-quality-{PROJECT_NUMBER}-source-as1"
CRM = "https://cloudresourcemanager.googleapis.com"
IAM = "https://iam.googleapis.com"
ORG = f"https://orgpolicy.googleapis.com/v2/projects/{PROJECT_NUMBER}"
AR = f"https://artifactregistry.googleapis.com/v1/projects/{PROJECT}/locations/{REGION}"
RUN = f"https://run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/jobs/{JOB}"
BASE_APIS = bootstrap.REQUIRED_APIS | {"storage.googleapis.com", "logging.googleapis.com"}
INSPECTION_APIS = {"orgpolicy.googleapis.com", "accesscontextmanager.googleapis.com"}
POLICY_TARGETS = {
    "gcp.resourceLocations",
    "iam.workloadIdentityPoolProviders",
    "iam.disableServiceAccountCreation",
    "iam.allowedPolicyMemberDomains",
    "iam.managed.allowedPolicyMembers",
    "gcp.restrictEndpointUsage",
    "gcp.restrictNonCmekServices",
    "gcp.restrictCmekCryptoKeyProjects",
    "run.requireVPCConnector",
    "run.allowedVPCEgress",
    "run.allowedBinaryAuthorizationPolicies",
}


class ProbeFailure(RuntimeError):
    def __init__(self, code=None, status="LOCAL_OR_TRANSPORT", reasons=()):
        self.code, self.status, self.reasons = code, status, tuple(reasons)
        super().__init__("Read-only probe failed; private details omitted")


class StopAuthentication(RuntimeError):
    pass


class CloudReader:
    """Only local credential inspection plus allowlisted, non-retried API reads."""

    def __init__(self):
        self.account = None
        self._token = None
        self.cache = {}

    def _once(self, key, operation):
        if key not in self.cache:
            try:
                self.cache[key] = (True, operation())
            except (review_read.ReadFailure, ProbeFailure) as error:
                self.cache[key] = (False, error)
            except (
                RuntimeError,
                OSError,
                ValueError,
                TypeError,
                KeyError,
                AttributeError,
                IndexError,
            ):
                self.cache[key] = (False, ProbeFailure(status="INVALID_READ_OR_LOCAL_RESPONSE"))
        success, value = self.cache[key]
        if not success:
            raise value
        return value

    def local(self, *args):
        allowed = {
            ("config", "get-value", "core/project"),
            ("config", "get-value", "auth/impersonate_service_account"),
            ("config", "get-value", "auth/credential_file_override"),
            ("config", "get-value", "auth/access_token_file"),
            ("auth", "list", "--filter=status:ACTIVE", "--format=json"),
            ("auth", "print-access-token"),
        }
        if args not in allowed:
            raise ProbeFailure(status="FORBIDDEN_COMMAND")

        def run():
            env = os.environ.copy()
            env.update(
                {
                    "CLOUDSDK_CORE_DISABLE_PROMPTS": "1",
                    "CLOUDSDK_CORE_DISABLE_FILE_LOGGING": "1",
                    "CLOUDSDK_CORE_LOG_HTTP": "0",
                    "CLOUDSDK_CORE_DISABLE_USAGE_REPORTING": "1",
                }
            )
            command = ["gcloud", "--quiet"]
            if self.account:
                command.append("--account=" + self.account)
            try:
                result = subprocess.run(
                    command + list(args),
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env=env,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                raise ProbeFailure(status="GCLOUD_UNAVAILABLE_OR_TIMEOUT") from None
            if result.returncode:
                raise ProbeFailure(status="GCLOUD_AUTH_OR_CONFIGURATION")
            return result.stdout.strip()

        return self._once(("local", args), run)

    def api(self, method, url, permissions=None):
        review_read.validate_request(method, url, permissions)

        def run():
            if self._token is None:
                self._token = self.local("auth", "print-access-token")
            if not self._token or any(c.isspace() for c in self._token):
                raise ProbeFailure(status="GCLOUD_AUTH_OR_CONFIGURATION")
            try:
                return review_read.request(method, url, permissions, token=self._token)
            except review_read.ReadFailure:
                raise
            except (RuntimeError, OSError, ValueError):
                raise ProbeFailure(status="TRANSPORT_OR_INVALID_RESPONSE") from None

        return self._once((method, url, tuple(permissions or [])), run)

    def pages(self, url, field):
        values, seen, token = [], set(), None
        while True:
            target = url + (
                ("&" if "?" in url else "?") + urlencode({"pageToken": token}) if token else ""
            )
            result = self.api("GET", target)
            entries = result.get(field, [])
            if not isinstance(entries, list):
                raise ProbeFailure(status="INVALID_INVENTORY")
            values.extend(entries)
            token = result.get("nextPageToken")
            if not token:
                return values
            if not isinstance(token, str) or token in seen:
                raise ProbeFailure(status="INCOMPLETE_PAGINATION")
            seen.add(token)

    def github(self, path):
        prefix = "repos/" + REPO
        allowed = {
            "user",
            prefix,
            prefix + "/branches/main",
            prefix + "/branches/main/protection",
            prefix + "/rules/branches/main",
        }
        env_prefixes = tuple(
            prefix + "/environments/" + name for name in ("gcp-deploy", "gcp-execute")
        )
        environment_inventory = path.startswith(prefix + "/environments?")
        if (
            path not in allowed
            and not environment_inventory
            and not any(
                path == p or path.startswith(p + "/deployment-branch-policies?")
                for p in env_prefixes
            )
        ):
            raise ProbeFailure(status="FORBIDDEN_GITHUB_ENDPOINT")

        def run():
            try:
                result = subprocess.run(
                    [
                        "gh",
                        "api",
                        "--method",
                        "GET",
                        path,
                        "--hostname",
                        "github.com",
                        "-H",
                        "X-GitHub-Api-Version: 2026-03-10",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=45,
                    check=False,
                    env={**os.environ, "GH_DEBUG": "0", "GH_PROMPT_DISABLED": "1"},
                )
            except (OSError, subprocess.SubprocessError):
                raise ProbeFailure(status="EXISTING_GITHUB_AUTH_UNAVAILABLE") from None
            if result.returncode:
                match = re.search(r"HTTP (\d{3})", result.stderr)
                if not match and "gh auth login" in result.stderr:
                    raise ProbeFailure(status="EXISTING_GITHUB_AUTH_UNAVAILABLE")
                raise ProbeFailure(
                    int(match.group(1)) if match else None, "GITHUB_READ_DENIED_OR_UNAVAILABLE"
                )
            try:
                return json.loads(result.stdout)
            except ValueError:
                raise ProbeFailure(status="INVALID_GITHUB_RESPONSE") from None

        return self._once(("github", path), run)

    def github_pages(self, path, field=None):
        values, total = [], None
        for page in range(1, 101):
            result = self.github(path + f"?per_page=100&page={page}")
            entries = result if field is None else result.get(field, [])
            if not isinstance(entries, list):
                raise ProbeFailure(status="INVALID_GITHUB_INVENTORY")
            values.extend(entries)
            if field:
                count = result.get("total_count")
                if type(count) is not int or (total is not None and count != total):
                    raise ProbeFailure(status="INCOMPLETE_GITHUB_INVENTORY")
                total = count
                if len(values) == total:
                    return values
            elif len(entries) < 100:
                return values
            if not entries:
                break
        raise ProbeFailure(status="INCOMPLETE_GITHUB_PAGINATION")


class Preflight:
    def __init__(self, reader, config):
        self.reader, self.config = reader, config
        self.results = []
        self.enabled = set()
        self.ancestors = []
        self.accounts = {}
        self.job = None
        self.github_auth_failed = False

    def add(self, category, check, status, detail):
        if category not in CATEGORIES or status not in STATUSES:
            raise ValueError("Invalid preflight classification")
        self.results.append(
            {"category": category, "check": check, "status": status, "detail": detail}
        )

    def probe(
        self, category, check, operation, expected_missing=False, missing_status="EXPECTED MISSING"
    ):
        if category == "github" and self.github_auth_failed:
            self.add(
                category,
                check,
                "UNVERIFIED",
                "GitHub authentication prerequisite failed; no further GitHub requests",
            )
            return None
        try:
            return operation()
        except (review_read.ReadFailure, ProbeFailure) as error:
            if (
                error.code == 404
                and (not isinstance(error, review_read.ReadFailure) or error.status == "NOT_FOUND")
                and expected_missing
            ):
                self.add(
                    category,
                    check,
                    missing_status,
                    (
                        "Planned resource/direct policy reported absent; no creation or repair"
                        if missing_status == "EXPECTED MISSING"
                        else "Resource/view not found; absence versus visibility requires review"
                    ),
                )
            else:
                status = (
                    "BLOCKED"
                    if error.code in (400, 401, 403, 404)
                    or error.status
                    in {
                        "EXISTING_GITHUB_AUTH_UNAVAILABLE",
                        "GCLOUD_AUTH_OR_CONFIGURATION",
                        "GCLOUD_UNAVAILABLE_OR_TIMEOUT",
                    }
                    else "UNVERIFIED"
                )
                reason = "SERVICE_DISABLED" if "SERVICE_DISABLED" in error.reasons else error.status
                safe = (
                    reason
                    if reason
                    in {
                        "SERVICE_DISABLED",
                        "PERMISSION_DENIED",
                        "NOT_FOUND",
                        "UNAUTHENTICATED",
                        "INVALID_ARGUMENT",
                        "LOCAL_OR_TRANSPORT",
                        "FORBIDDEN_COMMAND",
                        "GCLOUD_UNAVAILABLE_OR_TIMEOUT",
                        "GCLOUD_AUTH_OR_CONFIGURATION",
                        "TRANSPORT_OR_INVALID_RESPONSE",
                        "INVALID_INVENTORY",
                        "INCOMPLETE_PAGINATION",
                        "EXISTING_GITHUB_AUTH_UNAVAILABLE",
                        "GITHUB_READ_DENIED_OR_UNAVAILABLE",
                        "INVALID_GITHUB_RESPONSE",
                        "INVALID_GITHUB_INVENTORY",
                        "INCOMPLETE_GITHUB_INVENTORY",
                        "INCOMPLETE_GITHUB_PAGINATION",
                    }
                    else "PRIVATE_ERROR_OMITTED"
                )
                self.add(
                    category,
                    check,
                    status,
                    f"Read failed: HTTP {error.code}; {safe}; private details omitted",
                )
            if error.code == 401:
                if category == "github":
                    self.github_auth_failed = True
                else:
                    raise StopAuthentication from None
            return None
        except (RuntimeError, ValueError, TypeError, KeyError):
            self.add(
                category,
                check,
                "UNVERIFIED",
                "Read/response incomplete; private details omitted; no retry",
            )
            return None

    def api(self, category, check, url, *, missing=False):
        return self.probe(category, check, lambda: self.reader.api("GET", url), missing)

    def inventory(self, category, check, url, field):
        return self.probe(category, check, lambda: self.reader.pages(url, field))

    def need_api(self, category, name):
        if name not in self.enabled:
            self.add(
                category,
                "api." + name,
                "BLOCKED",
                "Inspection API not enabled/readable; never auto-enabled",
            )
            return False
        return True

    def context(self):
        inactive = self.config["enabled"] is False and not any(
            self.config["environment_bypass_disabled_attested"].values()
        )
        inactive = (
            inactive
            and self.config["approval_mode"] == "solo-owner"
            and self.config["solo_owner_approval_authorized"] is True
        )
        self.add(
            "context",
            "activation_gates",
            "PASS" if inactive else "BLOCKED",
            (
                "Design-only solo approval recorded; live activation/bypass "
                "attestations must remain disabled"
            ),
        )
        if not inactive:
            return False
        try:
            for key in (
                "CLOUDSDK_AUTH_ACCESS_TOKEN",
                "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE",
                "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT",
                "GOOGLE_APPLICATION_CREDENTIALS",
            ):
                if os.environ.get(key):
                    raise ProbeFailure(status="AUTH_OVERRIDE_PRESENT")
            for key in (
                "auth/impersonate_service_account",
                "auth/credential_file_override",
                "auth/access_token_file",
            ):
                if self.reader.local("config", "get-value", key) not in ("", "(unset)", "None"):
                    raise ProbeFailure(status="AUTH_OVERRIDE_PRESENT")
            if self.reader.local("config", "get-value", "core/project") != PROJECT:
                raise ProbeFailure(status="ACTIVE_PROJECT_MISMATCH")
            active = json.loads(
                self.reader.local("auth", "list", "--filter=status:ACTIVE", "--format=json")
            )
            if len(active) != 1 or active[0]["account"].endswith(".gserviceaccount.com"):
                raise ProbeFailure(status="EXPECTED_ONE_HUMAN_IDENTITY")
            self.reader.account = active[0]["account"]
        except (ProbeFailure, ValueError, KeyError, TypeError, AttributeError, IndexError):
            self.add(
                "context",
                "identity_project",
                "BLOCKED",
                (
                    "Missing/wrong human authentication, active project or auth "
                    "override; no cloud reads attempted"
                ),
            )
            return False
        project = self.api("context", "project_identity", CRM + f"/v3/projects/{PROJECT_NUMBER}")
        if (
            not project
            or project.get("projectId") != PROJECT
            or project.get("name") != "projects/" + PROJECT_NUMBER
            or project.get("state") != "ACTIVE"
        ):
            if project is not None:
                self.add(
                    "context",
                    "project_identity",
                    "BLOCKED",
                    "Wrong/inactive project or project number",
                )
            return False
        self.add(
            "context",
            "identity_project",
            "PASS",
            (
                "One active human, expected ACTIVE project/number and no "
                "overrides; identity identifier omitted"
            ),
        )
        parent, seen = project.get("parent"), set()
        while parent:
            if parent in seen or not re.fullmatch(r"(folders|organizations)/[0-9]+", parent):
                self.add(
                    "context",
                    "ancestry",
                    "UNVERIFIED",
                    "Invalid/repeated ancestor; administrator review required",
                )
                return False
            seen.add(parent)
            self.ancestors.append(parent)
            ancestor = self.api("context", "ancestor_visibility", CRM + "/v3/" + parent)
            if ancestor is None:
                break
            if ancestor.get("name") != parent:
                self.add(
                    "context",
                    "ancestor_shape",
                    "UNVERIFIED",
                    "Ancestor response identity incomplete; hierarchy not assumed complete",
                )
                break
            parent = ancestor.get("parent")
        self.add(
            "context",
            "ancestry_inventory",
            "PASS" if not parent else "UNVERIFIED",
            "Ancestor identifiers retained only in memory; inaccessible ancestors remain blocking",
        )
        billing = self.api(
            "context",
            "billing",
            f"https://cloudbilling.googleapis.com/v1/projects/{PROJECT}/billingInfo",
        )
        if billing is not None:
            self.add(
                "context",
                "billing",
                "PASS" if billing.get("billingEnabled") is True else "BLOCKED",
                "Billing readiness inspected; account details omitted",
            )
        services = self.inventory(
            "apis_quotas",
            "enabled_services",
            f"https://serviceusage.googleapis.com/v1/projects/{PROJECT_NUMBER}/services?filter=state%3AENABLED",
            "services",
        )
        if services is not None:
            self.enabled = {
                x.get("config", {}).get("name", x.get("name", "").rsplit("/", 1)[-1])
                for x in services
            }
        for name in sorted(BASE_APIS | INSPECTION_APIS | bootstrap.ENABLE_APIS):
            status = (
                "PASS"
                if name in self.enabled
                else ("EXPECTED MISSING" if name in bootstrap.ENABLE_APIS else "BLOCKED")
            )
            if services is None:
                status = "UNVERIFIED"
            self.add(
                "apis_quotas",
                "enabled." + name,
                status,
                (
                    "API inventory only; missing services are not enabled"
                    if services is not None
                    else "Enabled-service inventory inaccessible; API state unknown, not missing"
                ),
            )
        return True

    def policy(self, category, check, scope, resource, missing=False):
        if scope == "project":
            return self.probe(
                category,
                check,
                lambda: self.reader.api("POST", CRM + f"/v1/projects/{PROJECT}:getIamPolicy"),
                missing,
            )
        if scope == "service_account":
            url = IAM + f"/v1/projects/{PROJECT}/serviceAccounts/{resource}:getIamPolicy"
            return self.probe(category, check, lambda: self.reader.api("POST", url), missing)
        url = (
            AR + f"/repositories/{REPOSITORY}:getIamPolicy?options.requestedPolicyVersion=3"
            if scope == "repository"
            else RUN + ":getIamPolicy?options.requestedPolicyVersion=3"
        )
        return self.api(category, check, url, missing=missing)

    def permissions(self, check, url, permissions):
        result = self.probe("iam", check, lambda: self.reader.api("POST", url, permissions))
        if result is not None:
            complete = set(permissions) <= set(result.get("permissions", []))
            self.add(
                "iam",
                check,
                "PASS" if complete else "BLOCKED",
                "Current-caller permission query complete"
                if complete
                else "Current caller lacks requested effective permissions; no grants made",
            )

    def iam(self):
        if not self.need_api("iam", "iam.googleapis.com"):
            return
        for label, account in (
            ("runtime", RUNTIME_SA),
            ("build", BUILD_SA),
            ("deployer", DEPLOY_SA),
            ("executor", EXECUTE_SA),
        ):
            url = IAM + f"/v1/projects/{PROJECT}/serviceAccounts/{account}"
            record = self.api(
                "iam", "account." + label, url, missing=label in {"deployer", "executor"}
            )
            self.accounts[account] = record
            if record is None:
                continue
            good = (
                record.get("email") == account
                and record.get("projectId") == PROJECT
                and not record.get("disabled")
            )
            self.add(
                "iam",
                "account." + label,
                "PASS" if good else "BLOCKED",
                "Dedicated account inspected; no account changes",
            )
            keys = self.api("iam", "keys." + label, url + "/keys?keyTypes=USER_MANAGED")
            if keys is not None:
                self.add(
                    "iam",
                    "keys." + label,
                    "PASS" if not keys.get("keys") else "BLOCKED",
                    "User-managed key metadata checked; no key data emitted/downloaded",
                )
            if label in {"runtime", "build"}:
                required = ["iam.serviceAccounts.actAs"]
                if label == "runtime":
                    required += [
                        "iam.serviceAccounts.getIamPolicy",
                        "iam.serviceAccounts.setIamPolicy",
                    ]
                self.permissions("operator.actAs." + label, url + ":testIamPermissions", required)
        self.permissions(
            "operator.setup",
            CRM + f"/v1/projects/{PROJECT}:testIamPermissions",
            [
                "resourcemanager.projects.getIamPolicy",
                "resourcemanager.projects.setIamPolicy",
                "iam.roles.create",
                "iam.serviceAccounts.create",
                "iam.workloadIdentityPools.create",
                "iam.workloadIdentityPoolProviders.create",
                "serviceusage.services.enable",
                "run.jobs.create",
                "run.jobs.get",
            ],
        )
        project = self.policy("iam", "project_allow_policy", "project", PROJECT)
        if project is not None:
            for label, account, role in (
                (
                    "run_agent",
                    f"service-{PROJECT_NUMBER}@serverless-robot-prod.iam.gserviceaccount.com",
                    "roles/run.serviceAgent",
                ),
                (
                    "build_agent",
                    f"service-{PROJECT_NUMBER}@gcp-sa-cloudbuild.iam.gserviceaccount.com",
                    "roles/cloudbuild.serviceAgent",
                ),
            ):
                binding = {"role": role, "member": "serviceAccount:" + account}
                self.binding("iam", label, project, binding)
        role_name = f"projects/{PROJECT}/roles/syntheticQualityObjects"
        role = self.api("iam", "runtime_object_role", IAM + "/v1/" + role_name)
        if role is not None:
            good = (
                set(role.get("includedPermissions", []))
                == {"storage.objects.create", "storage.objects.get"}
                and not role.get("deleted")
                and role.get("stage") == "GA"
            )
            self.add(
                "iam",
                "runtime_object_role",
                "PASS" if good else "BLOCKED",
                "Existing runtime custom role compared with create/get only",
            )
        for label, bucket, account, expected_role in (
            ("data", DATA_BUCKET, RUNTIME_SA, role_name),
            ("source", SOURCE_BUCKET, BUILD_SA, "roles/storage.objectViewer"),
        ):
            url = "https://storage.googleapis.com/storage/v1/b/" + bucket
            record = self.api("iam", "bucket_settings." + label, url)
            if record is None:
                continue
            configuration = record.get("iamConfiguration", {})
            good = (
                record.get("location", "").upper() == REGION.upper()
                and record.get("storageClass") == "STANDARD"
                and configuration.get("publicAccessPrevention") == "enforced"
                and configuration.get("uniformBucketLevelAccess", {}).get("enabled") is True
            )
            self.add(
                "iam",
                "bucket_settings." + label,
                "PASS" if good else "BLOCKED",
                "Only planned private Mumbai/Standard buckets checked; unrelated bucket excluded",
            )
            self.add(
                "iam",
                "retention." + label,
                "UNVERIFIED",
                "Owner must review retained/soft-deleted data costs; no lifecycle/retention change",
            )
            policy = self.api(
                "iam", "bucket_policy." + label, url + "/iam?optionsRequestedPolicyVersion=3"
            )
            if policy is not None:
                self.binding(
                    "iam",
                    "stage1_binding." + label,
                    policy,
                    {"role": expected_role, "member": "serviceAccount:" + account},
                )
                self.identity_grants("iam", "bucket_privilege." + label, policy, "bucket", bucket)
        repository = self.api("iam", "repository", AR + f"/repositories/{REPOSITORY}")
        if repository is not None:
            policy = self.policy("iam", "repository_policy", "repository", REPOSITORY)
            if policy is not None:
                self.binding(
                    "iam",
                    "stage1_binding.repository",
                    policy,
                    {
                        "role": "roles/artifactregistry.writer",
                        "member": "serviceAccount:" + BUILD_SA,
                    },
                )
            self.permissions(
                "operator.repository",
                AR + f"/repositories/{REPOSITORY}:testIamPermissions",
                [
                    "artifactregistry.repositories.getIamPolicy",
                    "artifactregistry.repositories.setIamPolicy",
                ],
            )
        if project is not None:
            self.binding(
                "iam",
                "stage1_binding.logging",
                project,
                {"role": "roles/logging.logWriter", "member": "serviceAccount:" + BUILD_SA},
            )
        self.add(
            "iam",
            "effective_scope_limits",
            "UNVERIFIED",
            (
                "Direct/current-caller checks cannot prove all "
                "group/conditional/deny/PAB effects or future identity access; "
                "administrator review remains"
            ),
        )

    def organization_policy(self):
        if not self.need_api("organization_policy", "orgpolicy.googleapis.com"):
            return
        definitions = self.inventory(
            "organization_policy", "constraint_inventory", ORG + "/constraints", "constraints"
        )
        if not definitions:
            if definitions is not None:
                self.add(
                    "organization_policy",
                    "constraint_inventory",
                    "UNVERIFIED",
                    "Empty constraint inventory cannot classify policy support",
                )
            return
        known = {x.get("name", "").rsplit("/", 1)[-1] for x in definitions}
        targets = POLICY_TARGETS | {
            x
            for x in known
            if x.startswith(("custom.", "iam.managed.", "run.", "storage.", "artifactregistry."))
        }
        for index, name in enumerate(sorted(targets), 1):
            label = "constraint." + (str(index) if name.startswith("custom.") else name)
            if name not in known:
                self.add(
                    "organization_policy",
                    label,
                    "UNVERIFIED",
                    (
                        "UNSUPPORTED_OR_NOT_APPLICABLE in visible constraint definitions; "
                        "not treated as an unset policy"
                    ),
                )
                continue
            path = ORG + "/policies/" + quote(name, safe=".")
            local = self.api("organization_policy", label + ".local", path, missing=True)
            if local is not None:
                self.add(
                    "organization_policy",
                    label + ".local",
                    "PASS",
                    "Direct policy readable; private contents omitted",
                )
            effective = self.api(
                "organization_policy", label + ".effective", path + ":getEffectivePolicy"
            )
            if effective is None:
                continue
            rules = effective.get("spec", {}).get("rules", [])
            open_rules = bool(rules) and all(
                not r.get("condition") and (r.get("allowAll") is True or r.get("enforce") is False)
                for r in rules
            )
            self.add(
                "organization_policy",
                label + ".effective",
                "PASS" if open_rules else "UNVERIFIED",
                "Effective inherited/default policy read; unrestricted rules"
                if open_rules
                else (
                    "Conditional/list/custom/enforced restriction requires "
                    "administrator compatibility review; contents omitted"
                ),
            )
        for index, ancestor in enumerate(self.ancestors, 1):
            policy = self.probe(
                "organization_policy",
                f"ancestor_allow.{index}",
                lambda ancestor=ancestor: self.reader.api(
                    "POST", CRM + "/v3/" + ancestor + ":getIamPolicy"
                ),
            )
            if policy is not None:
                self.add(
                    "organization_policy",
                    f"ancestor_allow.{index}",
                    "PASS",
                    "Inherited allow policy readable; private principals/roles omitted",
                )

    def deny_pab(self):
        if not self.need_api("deny_pab", "iam.googleapis.com"):
            return
        for index, parent in enumerate(["projects/" + PROJECT_NUMBER] + self.ancestors, 1):
            attachment = quote("cloudresourcemanager.googleapis.com/" + parent, safe="")
            policies = self.inventory(
                "deny_pab",
                f"deny.{index}",
                IAM + "/v2/policies/" + attachment + "/denypolicies",
                "policies",
            )
            if policies is not None:
                self.add(
                    "deny_pab",
                    f"deny.{index}",
                    "PASS" if not policies else "UNVERIFIED",
                    "Complete empty deny inventory at this scope"
                    if not policies
                    else (
                        "Deny policies present; administrator must evaluate "
                        "rules/exceptions/conditions privately"
                    ),
                )
            bindings = self.inventory(
                "deny_pab",
                f"pab_bindings.{index}",
                IAM + "/v3/" + parent + "/locations/global/policyBindings",
                "policyBindings",
            )
            if bindings is not None:
                self.add(
                    "deny_pab",
                    f"pab_bindings.{index}",
                    "PASS" if not bindings else "UNVERIFIED",
                    "No PAB bindings visible at this scope"
                    if not bindings
                    else "PAB bindings present; administrator evaluation required",
                )
            if parent.startswith("organizations/"):
                definitions = self.inventory(
                    "deny_pab",
                    f"pab_policies.{index}",
                    IAM + "/v3/" + parent + "/locations/global/principalAccessBoundaryPolicies",
                    "principalAccessBoundaryPolicies",
                )
                if definitions is not None:
                    self.add(
                        "deny_pab",
                        f"pab_policies.{index}",
                        "PASS" if not definitions else "UNVERIFIED",
                        "PAB policy definitions inspected; private inventory omitted",
                    )
        self.add(
            "deny_pab",
            "visibility_limits",
            "UNVERIFIED",
            (
                "External principal/group/PAB bindings or inaccessible ancestors "
                "require administrator/Troubleshooter confirmation; emptiness is "
                "scope-limited"
            ),
        )

    def vpc_sc(self):
        organizations = [x for x in self.ancestors if x.startswith("organizations/")]
        if not organizations:
            self.add(
                "vpc_sc",
                "organization_scope",
                "UNVERIFIED",
                (
                    "No visible organization ancestor; confirm applicability/complete "
                    "ancestry with owner"
                ),
            )
            return
        if not self.need_api("vpc_sc", "accesscontextmanager.googleapis.com"):
            return
        for index, organization in enumerate(organizations, 1):
            root = "https://accesscontextmanager.googleapis.com/v1/"
            policies = self.inventory(
                "vpc_sc",
                f"access_policies.{index}",
                root + "accessPolicies?" + urlencode({"parent": organization}),
                "accessPolicies",
            )
            if policies is None:
                continue
            for pindex, policy in enumerate(policies, 1):
                name = policy.get("name", "")
                if not re.fullmatch(r"accessPolicies/[0-9]+", name):
                    self.add(
                        "vpc_sc",
                        "access_policy_shape",
                        "UNVERIFIED",
                        "Unexpected access-policy identifier",
                    )
                    continue
                perimeters = self.inventory(
                    "vpc_sc",
                    f"perimeters.{index}.{pindex}",
                    root + name + "/servicePerimeters",
                    "servicePerimeters",
                )
                levels = self.inventory(
                    "vpc_sc",
                    f"access_levels.{index}.{pindex}",
                    root + name + "/accessLevels",
                    "accessLevels",
                )
                if perimeters is not None and levels is not None:
                    member = any(
                        "projects/" + PROJECT_NUMBER in p.get(scope, {}).get("resources", [])
                        for p in perimeters
                        for scope in ("status", "spec")
                    )
                    self.add(
                        "vpc_sc",
                        f"membership.{index}.{pindex}",
                        "UNVERIFIED" if member else "PASS",
                        (
                            "Enforced/dry-run project membership found; "
                            "ingress/egress/bridge/identity review required"
                        )
                        if member
                        else (
                            "Project not in returned enforced/dry-run perimeters at this "
                            "visible policy"
                        ),
                    )
            if not policies:
                self.add(
                    "vpc_sc",
                    f"access_policies.{index}",
                    "PASS",
                    "Complete empty access-policy inventory at visible organization",
                )
        self.add(
            "vpc_sc",
            "runner_network_context",
            "UNVERIFIED",
            "Cloud Shell reads do not establish GitHub-hosted runner or job network compatibility",
        )

    def apis_quotas(self):
        if self.need_api("apis_quotas", "run.googleapis.com"):
            locations = self.inventory(
                "apis_quotas",
                "run_locations",
                f"https://run.googleapis.com/v2/projects/{PROJECT}/locations",
                "locations",
            )
            if locations is not None:
                supported = any(
                    x.get("locationId") == REGION or x.get("name", "").rsplit("/", 1)[-1] == REGION
                    for x in locations
                )
                self.add(
                    "apis_quotas",
                    "mumbai",
                    "PASS" if supported else "BLOCKED",
                    "Cloud Run location inventory checked; capacity not reserved",
                )
        for service in (
            "run.googleapis.com",
            "iam.googleapis.com",
            "iamcredentials.googleapis.com",
            "sts.googleapis.com",
        ):
            if service not in self.enabled:
                self.add(
                    "apis_quotas",
                    "quota." + service,
                    "UNVERIFIED",
                    "Service not enabled; quota inspection deferred without enablement",
                )
                continue
            metrics = self.inventory(
                "apis_quotas",
                "quota." + service,
                f"https://serviceusage.googleapis.com/v1beta1/projects/{PROJECT_NUMBER}/services/{service}/consumerQuotaMetrics?view=FULL",
                "metrics",
            )
            if metrics is not None:
                self.add(
                    "apis_quotas",
                    "quota." + service,
                    "UNVERIFIED",
                    (
                        "Limits readable; administrator must check units, "
                        "regional/current usage and 1 vCPU/1 GiB/one-task headroom; no "
                        "capacity guarantee"
                    ),
                )

    def artifact_registry(self):
        if not self.need_api("artifact_registry", "artifactregistry.googleapis.com"):
            return
        image = quote(IMAGE + "@" + self.config["image_digest"], safe="")
        record = self.api(
            "artifact_registry",
            "immutable_image",
            AR + f"/repositories/{REPOSITORY}/dockerImages/" + image,
        )
        if record is not None:
            expected = (
                f"{REGION}-docker.pkg.dev/{PROJECT}/{REPOSITORY}/{IMAGE}@"
                + self.config["image_digest"]
            )
            self.add(
                "artifact_registry",
                "immutable_image",
                "PASS" if record.get("uri") == expected else "BLOCKED",
                (
                    "Existing immutable metadata checked; no image/source download or "
                    "build verification repeated"
                ),
            )

    def github(self):
        who = self.probe("github", "identity", lambda: self.reader.github("user"))
        if who is None:
            return
        if str(who.get("id")) != OWNER_ID:
            self.add(
                "github",
                "identity",
                "BLOCKED",
                (
                    "Existing GitHub authentication is not the exact approved owner; "
                    "no alternate login attempted"
                ),
            )
            return
        self.add(
            "github",
            "identity",
            "PASS",
            "Existing GitHub owner identity verified; no token emitted",
        )
        prefix = "repos/" + REPO
        repo = self.probe("github", "repository", lambda: self.reader.github(prefix))
        if repo is None:
            return
        if (
            str(repo.get("id")) != REPO_ID
            or str(repo.get("owner", {}).get("id")) != OWNER_ID
            or repo.get("default_branch") != "main"
        ):
            self.add(
                "github",
                "repository",
                "BLOCKED",
                "Repository/owner/default-branch identity mismatch",
            )
            return
        self.add(
            "github",
            "repository",
            "PASS",
            "Exact repository/owner and main default branch verified",
        )
        branch = self.probe("github", "main", lambda: self.reader.github(prefix + "/branches/main"))
        if branch is not None:
            self.add(
                "github",
                "main",
                "PASS" if branch.get("protected") is True else "BLOCKED",
                "Protected-main flag checked; full effective rules still require review",
            )
        classic = self.probe(
            "github",
            "classic_protection",
            lambda: self.reader.github(prefix + "/branches/main/protection"),
            True,
            missing_status="UNVERIFIED",
        )
        if classic is not None:
            checks = classic.get("required_status_checks") or {}
            reviews = classic.get("required_pull_request_reviews")
            basic = (
                bool(checks.get("contexts") or checks.get("checks"))
                and isinstance(reviews, dict)
                and reviews.get("required_approving_review_count") == 0
                and reviews.get("require_code_owner_reviews") is not True
                and classic.get("enforce_admins", {}).get("enabled") is True
                and classic.get("allow_force_pushes", {}).get("enabled") is False
                and classic.get("allow_deletions", {}).get("enabled") is False
            )
            self.add(
                "github",
                "classic_protection",
                "PASS" if basic else "UNVERIFIED",
                "Basic solo-owner PR/CI/admin/force-push/deletion checks verified"
                if basic
                else (
                    "Classic rules require owner review of CI, PR governance, bypass "
                    "and force-push/deletion"
                ),
            )
        rules = self.probe(
            "github",
            "effective_rulesets",
            lambda: self.reader.github(prefix + "/rules/branches/main"),
        )
        if rules is not None:
            self.add(
                "github",
                "effective_rulesets",
                "UNVERIFIED",
                (
                    "Effective rules readable; review CI/PR/bypass requirements "
                    "privately; no settings changed"
                ),
            )
        environments = self.probe(
            "github",
            "environment_inventory",
            lambda: self.reader.github_pages(prefix + "/environments", "environments"),
        )
        for name in ("gcp-deploy", "gcp-execute"):
            if environments is None:
                self.add(
                    "github",
                    name,
                    "UNVERIFIED",
                    "Environment inventory inaccessible; planned environment absence not inferred",
                )
                continue
            if not any(item.get("name") == name for item in environments):
                self.add(
                    "github",
                    name,
                    "EXPECTED MISSING",
                    "Planned environment not in complete visible inventory; no creation",
                )
                continue
            path = prefix + "/environments/" + name
            env = self.probe("github", name, lambda path=path: self.reader.github(path))
            if env is None:
                continue
            policies = self.probe(
                "github",
                name + ".branch_rules",
                lambda path=path: self.reader.github_pages(
                    path + "/deployment-branch-policies", "branch_policies"
                ),
            )
            reviewers = [
                r for r in env.get("protection_rules", []) if r.get("type") == "required_reviewers"
            ]
            eligible = reviewers[0].get("reviewers", []) if len(reviewers) == 1 else []
            good = (
                env.get("name") == name
                and len(eligible) == 1
                and eligible[0].get("type") == "User"
                and str(eligible[0].get("reviewer", {}).get("id")) == OWNER_ID
                and reviewers[0].get("prevent_self_review") is False
            )
            self.add(
                "github",
                name + ".reviewer",
                "PASS" if good else "BLOCKED",
                (
                    "Exact solo-owner reviewer/self-review setting inspected; policy "
                    "design approval is not a live-settings change"
                ),
            )
            branch_policy = env.get("deployment_branch_policy", {})
            if policies is not None:
                selected = (
                    branch_policy.get("protected_branches") is False
                    and branch_policy.get("custom_branch_policies") is True
                )
                good = (
                    selected
                    and len(policies) == 1
                    and policies[0].get("name") == "main"
                    and policies[0].get("type") == "branch"
                )
                self.add(
                    "github",
                    name + ".branch_rules",
                    "PASS" if good else "BLOCKED",
                    "Exactly one selected main branch rule required; no tags/wildcards",
                )
            bypass = env.get("can_admins_bypass")
            status = "PASS" if bypass is False else ("BLOCKED" if bypass is True else "UNVERIFIED")
            self.add(
                "github",
                name + ".bypass",
                status,
                "Disabled admin bypass reported"
                if bypass is False
                else (
                    "Bypass enabled or omitted by API; owner UI verification "
                    "required; attestations remain false"
                ),
            )

    def wif(self):
        if not self.need_api("wif", "iam.googleapis.com"):
            return
        plan = bootstrap.identity_plan()
        for index, (kind, url, expected, _) in enumerate(bootstrap.resource_specs(plan), 1):
            record = self.api("wif", f"resource.{kind}.{index}", url, missing=True)
            if record is None:
                continue
            try:
                bootstrap.validate_resource(kind, record, expected)
                self.add(
                    "wif",
                    f"resource.{kind}.{index}",
                    "PASS",
                    "Existing dedicated resource matches exact reviewed specification",
                )
            except RuntimeError:
                self.add(
                    "wif",
                    f"resource.{kind}.{index}",
                    "BLOCKED",
                    "Existing role/account/pool/provider differs; no repair or adoption",
                )
            if kind == "pool":
                providers = self.inventory(
                    "wif",
                    f"pool_providers.{index}",
                    url + "/providers",
                    "workloadIdentityPoolProviders",
                )
                if providers is not None:
                    good = all(p.get("name") == expected["provider"]["name"] for p in providers)
                    self.add(
                        "wif",
                        f"pool_isolation.{index}",
                        "PASS" if good else "BLOCKED",
                        "Extra-provider principalSet crossover checked",
                    )
        self.job = self.api("wif", "job", RUN, missing=True)
        if self.job is not None:
            try:
                validate_job(self.job, self.config)
                if self.job.get("executionCount", 0):
                    raise RuntimeError("History")
                self.add(
                    "wif",
                    "job",
                    "PASS",
                    "Exact existing job configuration/history verified; never updated or executed",
                )
            except RuntimeError:
                self.add(
                    "wif",
                    "job",
                    "BLOCKED",
                    "Unexpected job configuration or execution history; no adoption/update",
                )
        for index, binding in enumerate(plan["bindings"], 1):
            if binding["scope"] == "job" and self.job is None:
                known_missing = any(
                    r["category"] == "wif"
                    and r["check"] == "job"
                    and r["status"] == "EXPECTED MISSING"
                    for r in self.results
                )
                self.add(
                    "wif",
                    f"binding.{index}",
                    "EXPECTED MISSING" if known_missing else "UNVERIFIED",
                    (
                        "Exact-job execution binding deferred; missing job or "
                        "inaccessible job policy is not repaired"
                    ),
                )
                continue
            if (
                binding["scope"] == "service_account"
                and binding["resource"] in {DEPLOY_SA, EXECUTE_SA}
                and self.accounts.get(binding["resource"]) is None
            ):
                label = "deployer" if binding["resource"] == DEPLOY_SA else "executor"
                known_missing = any(
                    r["category"] == "iam"
                    and r["check"] == "account." + label
                    and r["status"] == "EXPECTED MISSING"
                    for r in self.results
                )
                self.add(
                    "wif",
                    f"binding.{index}",
                    "EXPECTED MISSING" if known_missing else "UNVERIFIED",
                    (
                        "Dedicated account policy cannot be checked until account "
                        "existence/access is established"
                    ),
                )
                continue
            policy = self.policy(
                "wif",
                f"binding.{index}",
                binding["scope"],
                binding["resource"],
                missing=binding["resource"] in {DEPLOY_SA, EXECUTE_SA},
            )
            if policy is not None:
                self.identity_grants(
                    "wif", f"identity_scope.{index}", policy, binding["scope"], binding["resource"]
                )
                try:
                    present = bootstrap.binding_present(policy, binding)
                    self.add(
                        "wif",
                        f"binding.{index}",
                        "PASS" if present else "EXPECTED MISSING",
                        "Exact unconditional planned binding inspected; no grants made",
                    )
                except RuntimeError:
                    self.add(
                        "wif",
                        f"binding.{index}",
                        "BLOCKED",
                        "Conditional binding ambiguity; never broadened",
                    )
        self.add(
            "wif",
            "token_exchange",
            "UNVERIFIED",
            (
                "No live OIDC exchange/impersonation or IAM propagation test; "
                "disabled workflows remain disabled"
            ),
        )

    def binding(self, category, check, policy, expected, missing_status="BLOCKED"):
        try:
            present = bootstrap.binding_present(policy, expected)
            self.add(
                category,
                check,
                "PASS" if present else missing_status,
                "Exact unconditional binding inspected; no grants/repairs performed",
            )
        except RuntimeError:
            self.add(category, check, "BLOCKED", "Conditional grant ambiguity; never broadened")

    def identity_grants(self, category, check, policy, scope, resource):
        plan = bootstrap.identity_plan()
        good = True
        for grant in policy.get("bindings", []):
            for member in grant.get("members", []):
                if scope == "project" and any(
                    member.startswith(s + "iam.googleapis.com/" + p["name"] + "/")
                    for s in ("principalSet://", "principal://")
                    for p in plan["pools"]
                ):
                    good = False
                if resource in {DEPLOY_SA, EXECUTE_SA} and scope == "service_account":
                    allowed = [
                        b
                        for b in plan["bindings"]
                        if b["scope"] == scope and b["resource"] == resource
                    ]
                    if not any(
                        member == b["member"]
                        and grant.get("role") == b["role"]
                        and not grant.get("condition")
                        for b in allowed
                    ):
                        good = False
                if member in {"serviceAccount:" + DEPLOY_SA, "serviceAccount:" + EXECUTE_SA}:
                    if not any(
                        b["scope"] == scope
                        and b["resource"] == resource
                        and b["member"] == member
                        and b["role"] == grant.get("role")
                        and not grant.get("condition")
                        for b in plan["bindings"]
                    ):
                        good = False
        self.add(
            category,
            check,
            "PASS" if good else "BLOCKED",
            (
                "Dedicated identity grants checked against exact reviewed scopes; "
                "broader trust/privileges never repaired"
            ),
        )

    def run(self):
        try:
            ready = (
                False
                if any(
                    r["category"] == "context" and r["status"] == "BLOCKED" for r in self.results
                )
                else self.context()
            )
            if ready:
                for name in CATEGORIES[1:]:
                    try:
                        getattr(self, name)()
                    except StopAuthentication:
                        raise
                    except (
                        RuntimeError,
                        ValueError,
                        TypeError,
                        KeyError,
                        AttributeError,
                        IndexError,
                    ):
                        self.add(
                            name,
                            "completion",
                            "UNVERIFIED",
                            "Inspection incomplete; private details omitted; no retry/fix",
                        )
        except StopAuthentication:
            self.add(
                "context",
                "authentication_expired",
                "BLOCKED",
                "Authentication failed; all further GCP/GitHub probes stopped",
            )
            ready = False
        for name in CATEGORIES:
            if not any(r["category"] == name for r in self.results):
                self.add(
                    name,
                    "not_inspected",
                    "UNVERIFIED",
                    "Prerequisite/authentication visibility missing; no bypass attempted",
                )
        precedence = {"PASS": 0, "EXPECTED MISSING": 1, "UNVERIFIED": 2, "BLOCKED": 3}
        categories = {
            name: max(
                (r["status"] for r in self.results if r["category"] == name), key=precedence.get
            )
            for name in CATEGORIES
        }
        return {
            "project": PROJECT,
            "region": REGION,
            "read_only": True,
            "live_changes_authorized": False,
            "deployment_enabled": self.config["enabled"],
            "policy_design": self.config["approval_mode"],
            "categories": categories,
            "counts": dict(Counter(r["status"] for r in self.results)),
            "checks": self.results,
            "limits": (
                "Summary only; no policy inventories or credentials "
                "written/emitted. PASS is scoped to the stated check, never "
                "deployment approval."
            ),
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check",))
    parser.parse_args()
    config = load_config()
    preflight = Preflight(CloudReader(), config)
    evidence = json.loads(Path(__file__).with_name("source-evidence.json").read_text())
    _, hashes = validate_evidence(evidence, config)
    report = evidence.get("owner_run_verification", {})
    good = (
        report.get("status") == "ARCHIVE_CONTENTS_MATCH_PINNED_APPLICATION"
        and report.get("matched_files") == len(expected_sources())
        and report.get("application_commit") == APP_REV
        and report.get("source_archive_sha256") == hashes["SHA256"]
        and report.get("replacement_build_id") == config["replacement_build_id"]
        and report.get("image_uri") == evidence["reported_image_uri"]
    )
    preflight.add(
        "context",
        "recorded_source_evidence",
        "PASS" if good else "BLOCKED",
        (
            "Recorded owner-run evidence only; no source/build verification "
            "repeated and no signed provenance claim"
        ),
    )
    result = preflight.run()
    print(json.dumps(result, indent=2))
    return (
        2 if result["counts"].get("BLOCKED") else (1 if result["counts"].get("UNVERIFIED") else 0)
    )


def entrypoint():
    try:
        return main()
    except (RuntimeError, ValueError, OSError, KeyError, TypeError, AttributeError, IndexError):
        statuses = {name: "BLOCKED" if name == "context" else "UNVERIFIED" for name in CATEGORIES}
        print(
            json.dumps(
                {
                    "project": PROJECT,
                    "read_only": True,
                    "live_changes_authorized": False,
                    "categories": statuses,
                    "checks": [
                        {
                            "category": name,
                            "check": "local_prerequisite",
                            "status": status,
                            "detail": (
                                "Local configuration/evidence unavailable; "
                                "private details omitted; no bypass"
                            ),
                        }
                        for name, status in statuses.items()
                    ],
                }
            )
        )
        return 2


if __name__ == "__main__":
    sys.exit(entrypoint())
