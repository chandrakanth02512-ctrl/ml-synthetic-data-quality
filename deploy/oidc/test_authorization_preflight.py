"""Offline collector integration tests with synthetic private-policy fixtures."""

import copy
import io
import json
import unittest
from unittest.mock import patch

import authorization_preflight as pre
import review_read
import settings


class FakeCloud:
    def __init__(self):
        self.calls = []
        self.account = None
        self.active_project = pre.PROJECT
        self.config = settings.load_config()
        self.failures = {}
        self.overrides = {}
        self.present_wif = False
        self.enforced_policy = False
        self.env_missing = False
        self.bypass = False
        self.unsafe_branch = False
        self.private = "DO_NOT_EMIT_PRIVATE_POLICY_OR_CREDENTIAL"

    def local(self, *args):
        self.calls.append(("local", args))
        if args == ("config", "get-value", "core/project"):
            return self.active_project
        if args[:2] == ("config", "get-value"):
            return "(unset)"
        if args[:2] == ("auth", "list"):
            return json.dumps([{"account": self.private + "@example.invalid"}])
        raise AssertionError("No real credentials/processes in fake")

    def project_policy(self):
        return {
            "bindings": [
                {
                    "role": "roles/run.serviceAgent",
                    "members": [
                        f"serviceAccount:service-{pre.PROJECT_NUMBER}@serverless-robot-prod.iam.gserviceaccount.com"
                    ],
                },
                {
                    "role": "roles/cloudbuild.serviceAgent",
                    "members": [
                        f"serviceAccount:service-{pre.PROJECT_NUMBER}@gcp-sa-cloudbuild.iam.gserviceaccount.com"
                    ],
                },
                {"role": "roles/logging.logWriter", "members": ["serviceAccount:" + pre.BUILD_SA]},
                {"role": "roles/viewer", "members": ["user:" + self.private]},
            ]
        }

    def api(self, method, url, permissions=None):
        review_read.validate_request(method, url, permissions)
        self.calls.append((method, url, permissions))
        if url in self.failures:
            raise self.failures[url]
        if url in self.overrides:
            return copy.deepcopy(self.overrides[url])
        if url.endswith(":testIamPermissions"):
            return {"permissions": list(permissions)}
        if url == pre.CRM + f"/v3/projects/{pre.PROJECT_NUMBER}":
            return {
                "name": "projects/" + pre.PROJECT_NUMBER,
                "projectId": pre.PROJECT,
                "state": "ACTIVE",
                "parent": "folders/123",
            }
        if url == pre.CRM + "/v3/folders/123":
            return {"name": "folders/123", "parent": "organizations/456"}
        if url == pre.CRM + "/v3/organizations/456":
            return {"name": "organizations/456"}
        if "cloudbilling.googleapis.com" in url:
            return {"billingEnabled": True, "billingAccountName": self.private}
        if url.endswith(":getIamPolicy") or ":getIamPolicy?" in url:
            if "cloudresourcemanager" in url:
                return self.project_policy()
            if "artifactregistry" in url:
                return {
                    "bindings": [
                        {
                            "role": "roles/artifactregistry.writer",
                            "members": ["serviceAccount:" + pre.BUILD_SA],
                        }
                    ]
                }
            return {"bindings": []}
        if "storage.googleapis.com" in url:
            bucket = pre.DATA_BUCKET if pre.DATA_BUCKET in url else pre.SOURCE_BUCKET
            if "/iam?" in url:
                role = (
                    f"projects/{pre.PROJECT}/roles/syntheticQualityObjects"
                    if bucket == pre.DATA_BUCKET
                    else "roles/storage.objectViewer"
                )
                account = pre.RUNTIME_SA if bucket == pre.DATA_BUCKET else pre.BUILD_SA
                return {"bindings": [{"role": role, "members": ["serviceAccount:" + account]}]}
            return {
                "location": pre.REGION.upper(),
                "storageClass": "STANDARD",
                "iamConfiguration": {
                    "publicAccessPrevention": "enforced",
                    "uniformBucketLevelAccess": {"enabled": True},
                },
            }
        if url.endswith("/keys?keyTypes=USER_MANAGED"):
            return {}
        if "/serviceAccounts/" in url:
            email = url.rsplit("/", 1)[-1]
            if email in {pre.DEPLOY_SA, pre.EXECUTE_SA} and not self.present_wif:
                raise review_read.ReadFailure(404, "NOT_FOUND")
            return {"email": email, "projectId": pre.PROJECT, "disabled": False}
        if url.endswith("/roles/syntheticQualityObjects"):
            return {
                "stage": "GA",
                "includedPermissions": ["storage.objects.create", "storage.objects.get"],
            }
        if url.endswith(":getEffectivePolicy"):
            rule = (
                {"enforce": True, "condition": {"expression": self.private}}
                if self.enforced_policy
                else {"allowAll": True}
            )
            return {"spec": {"rules": [rule]}}
        if "/policies/" in url and "orgpolicy" in url:
            raise review_read.ReadFailure(404, "NOT_FOUND")
        if "/dockerImages/" in url:
            return {"uri": settings.image_uri(self.config)}
        if url == pre.AR + f"/repositories/{pre.REPOSITORY}":
            return {"name": self.private}
        if url == pre.RUN:
            raise review_read.ReadFailure(404, "NOT_FOUND")
        for kind, target, expected, _ in pre.bootstrap.resource_specs(
            pre.bootstrap.identity_plan()
        ):
            if url != target:
                continue
            if not self.present_wif:
                raise review_read.ReadFailure(404, "NOT_FOUND")
            if kind == "role":
                return {
                    "name": expected["name"],
                    "includedPermissions": expected["permissions"],
                    "stage": "GA",
                }
            if kind == "pool":
                return {"name": expected["name"], "state": "ACTIVE"}
            if kind == "provider":
                return {
                    "name": expected["name"],
                    "state": "ACTIVE",
                    "attributeMapping": expected["attributeMapping"],
                    "attributeCondition": expected["attributeCondition"],
                    "oidc": {
                        "issuerUri": expected["issuerUri"],
                        "allowedAudiences": expected["allowedAudiences"],
                    },
                }
        raise AssertionError("Unmodelled fake read: " + url)

    def pages(self, url, field):
        self.calls.append(("pages", url, field))
        if url in self.failures:
            raise self.failures[url]
        if url in self.overrides:
            return copy.deepcopy(self.overrides[url])
        if field == "services":
            names = pre.BASE_APIS | pre.INSPECTION_APIS | pre.bootstrap.ENABLE_APIS
            return [{"config": {"name": n}} for n in names]
        if field == "constraints":
            return [
                {"name": "constraints/" + n}
                for n in pre.POLICY_TARGETS | {"custom." + self.private}
            ]
        if field == "accessPolicies":
            return [{"name": "accessPolicies/7", "title": self.private}]
        if field == "locations":
            return [{"locationId": pre.REGION}]
        if field == "metrics":
            return [{"metric": self.private}]
        return []

    def github(self, path):
        self.calls.append(("github", path))
        if path in self.failures:
            raise self.failures[path]
        if path in self.overrides:
            return copy.deepcopy(self.overrides[path])
        if path == "user":
            return {"id": int(pre.OWNER_ID), "login": pre.REPO.split("/")[0]}
        if path == "repos/" + pre.REPO:
            return {
                "id": int(pre.REPO_ID),
                "owner": {"id": int(pre.OWNER_ID)},
                "default_branch": "main",
            }
        if path.endswith("/branches/main"):
            return {"name": "main", "protected": not self.unsafe_branch}
        if path.endswith("/protection"):
            return {
                "required_status_checks": {"contexts": ["checks", "security"]},
                "required_pull_request_reviews": {"required_approving_review_count": 0},
                "enforce_admins": {"enabled": True},
                "allow_force_pushes": {"enabled": False},
                "allow_deletions": {"enabled": False},
            }
        if path.endswith("/rules/branches/main"):
            return []
        if "/environments/" in path:
            if self.env_missing:
                raise pre.ProbeFailure(404, "NOT_FOUND")
            return {
                "name": path.rsplit("/", 1)[-1],
                "can_admins_bypass": self.bypass,
                "protection_rules": [
                    {
                        "type": "required_reviewers",
                        "prevent_self_review": False,
                        "reviewers": [{"type": "User", "reviewer": {"id": int(pre.OWNER_ID)}}],
                    }
                ],
                "deployment_branch_policy": {
                    "protected_branches": False,
                    "custom_branch_policies": True,
                },
            }
        raise AssertionError("Unmodelled GitHub read")

    def github_pages(self, path, field=None):
        self.calls.append(("github_pages", path, field))
        if path in self.failures:
            raise self.failures[path]
        if field == "environments":
            return (
                []
                if self.env_missing
                else [{"name": name} for name in ("gcp-deploy", "gcp-execute")]
            )
        return [{"name": "main", "type": "branch"}]


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.cloud = FakeCloud()
        self.config = settings.load_config()
        self.stack = patch.dict("os.environ", {}, clear=True)
        self.stack.start()
        self.addCleanup(self.stack.stop)
        self.network = patch("socket.socket", side_effect=AssertionError("No network"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def run_report(self):
        return pre.Preflight(self.cloud, self.config).run()

    def checks(self, report, name):
        return [r for r in report["checks"] if r["check"] == name]

    def test_full_fixture_is_summary_only_and_never_deployment_approval(self):
        report = self.run_report()
        self.assertEqual(set(report["categories"]), set(pre.CATEGORIES))
        self.assertTrue(all(r["status"] in pre.STATUSES for r in report["checks"]))
        self.assertNotIn(self.cloud.private, json.dumps(report))
        self.assertFalse(report["deployment_enabled"])
        self.assertFalse(report["live_changes_authorized"])
        self.assertEqual(report["policy_design"], "solo-owner")
        self.assertTrue(report["counts"].get("EXPECTED MISSING"))
        self.assertTrue(report["counts"].get("UNVERIFIED"))
        self.assertFalse(report["counts"].get("BLOCKED"))

    def test_wrong_active_project_stops_before_cloud_or_github_calls(self):
        self.cloud.active_project = "unrelated"
        report = self.run_report()
        self.assertEqual(report["categories"]["context"], "BLOCKED")
        self.assertTrue(all(c[0] == "local" for c in self.cloud.calls))
        self.assertEqual(set(report["categories"]), set(pre.CATEGORIES))

    def test_enabled_snapshot_is_blocked_before_any_live_probe(self):
        self.config["enabled"] = True
        report = self.run_report()
        self.assertEqual(report["categories"]["context"], "BLOCKED")
        self.assertFalse(self.cloud.calls)

    def test_invalid_local_evidence_produces_redacted_json_for_all_categories(self):
        output = io.StringIO()
        with patch.object(pre, "load_config", side_effect=ValueError(self.cloud.private)):
            with (
                patch("sys.stdout", output),
                patch("sys.argv", ["authorization_preflight.py", "check"]),
            ):
                self.assertEqual(pre.entrypoint(), 2)
        report = json.loads(output.getvalue())
        self.assertEqual(set(report["categories"]), set(pre.CATEGORIES))
        self.assertNotIn(self.cloud.private, output.getvalue())

    def test_cli_entrypoint_prints_only_redacted_fixture_report(self):
        output = io.StringIO()
        with patch.object(pre, "CloudReader", return_value=self.cloud):
            with (
                patch("sys.stdout", output),
                patch("sys.argv", ["authorization_preflight.py", "check"]),
            ):
                self.assertEqual(pre.entrypoint(), 1)
        report = json.loads(output.getvalue())
        self.assertEqual(set(report["categories"]), set(pre.CATEGORIES))
        self.assertFalse(report["deployment_enabled"])
        self.assertNotIn(self.cloud.private, output.getvalue())

    def test_auth_override_blocks_without_exposing_value(self):
        with patch.dict("os.environ", {"CLOUDSDK_AUTH_ACCESS_TOKEN": self.cloud.private}):
            report = self.run_report()
        self.assertFalse(self.cloud.calls)
        self.assertNotIn(self.cloud.private, json.dumps(report))

    def test_permission_denial_is_blocked_not_missing_and_other_scopes_continue(self):
        target = pre.IAM + f"/v1/projects/{pre.PROJECT}/serviceAccounts/{pre.DEPLOY_SA}"
        self.cloud.failures[target] = review_read.ReadFailure(403, "PERMISSION_DENIED")
        report = self.run_report()
        self.assertEqual(self.checks(report, "account.deployer")[0]["status"], "BLOCKED")
        self.assertTrue(self.checks(report, "immutable_image"))
        self.assertTrue(self.checks(report, "gcp-deploy.reviewer"))

    def test_disabled_inspection_api_is_reported_and_not_called(self):
        url = f"https://serviceusage.googleapis.com/v1/projects/{pre.PROJECT_NUMBER}/services?filter=state%3AENABLED"
        names = pre.BASE_APIS | pre.bootstrap.ENABLE_APIS
        self.cloud.overrides[url] = [{"config": {"name": n}} for n in names]
        report = self.run_report()
        self.assertEqual(report["categories"]["organization_policy"], "BLOCKED")
        self.assertFalse(
            any(
                "orgpolicy.googleapis.com" in str(c[1]) for c in self.cloud.calls if c[0] != "local"
            )
        )

    def test_expected_local_policy_absence_still_checks_effective_policy(self):
        report = self.run_report()
        label = "constraint.gcp.resourceLocations"
        self.assertEqual(self.checks(report, label + ".local")[0]["status"], "EXPECTED MISSING")
        self.assertEqual(self.checks(report, label + ".effective")[0]["status"], "PASS")

    def test_unsupported_constraint_is_distinct_from_unset_or_denied(self):
        self.cloud.overrides[pre.ORG + "/constraints"] = [{"name": "constraints/custom.private"}]
        report = self.run_report()
        check = self.checks(report, "constraint.gcp.resourceLocations")[0]
        self.assertEqual(check["status"], "UNVERIFIED")
        self.assertIn("UNSUPPORTED", check["detail"])

    def test_conditional_private_policy_is_not_printed_or_auto_approved(self):
        self.cloud.enforced_policy = True
        report = self.run_report()
        self.assertEqual(report["categories"]["organization_policy"], "UNVERIFIED")
        self.assertNotIn(self.cloud.private, json.dumps(report))

    def test_deny_presence_requires_review_and_hidden_ancestor_is_blocking(self):
        self.cloud.overrides[
            pre.IAM
            + "/v2/policies/cloudresourcemanager.googleapis.com%2Fprojects%2F"
            + pre.PROJECT_NUMBER
            + "/denypolicies"
        ] = [{"details": self.cloud.private}]
        self.cloud.failures[pre.CRM + "/v3/folders/123"] = review_read.ReadFailure(
            403, "PERMISSION_DENIED"
        )
        report = self.run_report()
        self.assertEqual(report["categories"]["context"], "BLOCKED")
        self.assertEqual(self.checks(report, "deny.1")[0]["status"], "UNVERIFIED")
        self.assertNotIn(self.cloud.private, json.dumps(report))

    def test_vpc_perimeter_membership_requires_network_review(self):
        url = "https://accesscontextmanager.googleapis.com/v1/accessPolicies/7/servicePerimeters"
        self.cloud.overrides[url] = [
            {
                "status": {"resources": ["projects/" + pre.PROJECT_NUMBER]},
                "name": self.cloud.private,
            }
        ]
        report = self.run_report()
        self.assertEqual(self.checks(report, "membership.1.1")[0]["status"], "UNVERIFIED")

    def test_quota_limits_are_not_claimed_as_current_usage_headroom(self):
        report = self.run_report()
        self.assertEqual(self.checks(report, "quota.run.googleapis.com")[0]["status"], "UNVERIFIED")

    def test_missing_future_environments_are_expected_without_creating_them(self):
        self.cloud.env_missing = True
        report = self.run_report()
        self.assertEqual(self.checks(report, "gcp-deploy")[0]["status"], "EXPECTED MISSING")
        self.assertFalse(any("create" in str(c) for c in self.cloud.calls if c[0] == "github"))

    def test_inaccessible_environment_inventory_is_not_false_absence(self):
        self.cloud.failures["repos/" + pre.REPO + "/environments"] = pre.ProbeFailure(
            403, "PERMISSION_DENIED"
        )
        report = self.run_report()
        self.assertEqual(self.checks(report, "environment_inventory")[0]["status"], "BLOCKED")
        self.assertEqual(self.checks(report, "gcp-deploy")[0]["status"], "UNVERIFIED")

    def test_denied_service_inventory_leaves_optional_api_state_unknown(self):
        url = f"https://serviceusage.googleapis.com/v1/projects/{pre.PROJECT_NUMBER}/services?filter=state%3AENABLED"
        self.cloud.failures[url] = review_read.ReadFailure(403, "PERMISSION_DENIED")
        report = self.run_report()
        self.assertEqual(
            self.checks(report, "enabled.sts.googleapis.com")[0]["status"], "UNVERIFIED"
        )

    def test_environment_bypass_and_main_protection_fail_closed(self):
        self.cloud.bypass = True
        self.cloud.unsafe_branch = True
        report = self.run_report()
        self.assertEqual(self.checks(report, "gcp-deploy.bypass")[0]["status"], "BLOCKED")
        self.assertEqual(self.checks(report, "main")[0]["status"], "BLOCKED")

    def test_omitted_bypass_is_unverified_not_attested_automatically(self):
        self.cloud.bypass = None
        report = self.run_report()
        self.assertEqual(self.checks(report, "gcp-deploy.bypass")[0]["status"], "UNVERIFIED")
        self.assertFalse(any(self.config["environment_bypass_disabled_attested"].values()))

    def test_github_auth_failure_does_not_prevent_independent_wif_reads(self):
        self.cloud.failures["user"] = pre.ProbeFailure(401, "GITHUB_AUTH")
        report = self.run_report()
        self.assertEqual(report["categories"]["github"], "BLOCKED")
        self.assertTrue(self.checks(report, "token_exchange"))

    def test_gcp_auth_failure_stops_all_remaining_requests(self):
        self.cloud.failures[pre.CRM + f"/v3/projects/{pre.PROJECT_NUMBER}"] = (
            review_read.ReadFailure(401, "UNAUTHENTICATED")
        )
        report = self.run_report()
        self.assertEqual(report["categories"]["context"], "BLOCKED")
        self.assertFalse(any(c[0] == "github" for c in self.cloud.calls))
        self.assertEqual(len([c for c in self.cloud.calls if c[0] in {"GET", "POST", "pages"}]), 1)

    def test_job_denied_is_not_reported_as_absent_for_execution_binding(self):
        self.cloud.failures[pre.RUN] = review_read.ReadFailure(403, "PERMISSION_DENIED")
        report = self.run_report()
        self.assertEqual(self.checks(report, "job")[0]["status"], "BLOCKED")
        self.assertEqual(self.checks(report, "binding.6")[0]["status"], "UNVERIFIED")

    def test_project_pool_grant_crossover_is_blocked_without_repair(self):
        collector = pre.Preflight(self.cloud, self.config)
        pool = pre.bootstrap.identity_plan()["pools"][0]["name"]
        collector.identity_grants(
            "wif",
            "scope",
            {
                "bindings": [
                    {
                        "role": "roles/iam.workloadIdentityUser",
                        "members": ["principalSet://iam.googleapis.com/" + pool + "/*"],
                    }
                ]
            },
            "project",
            pre.PROJECT,
        )
        self.assertEqual(collector.results[0]["status"], "BLOCKED")

    def test_source_prerequisite_block_stops_collection_without_reverification(self):
        collector = pre.Preflight(self.cloud, self.config)
        collector.add("context", "source", "BLOCKED", "Fixture mismatch")
        result = collector.run()
        self.assertFalse(self.cloud.calls)
        self.assertEqual(set(result["categories"]), set(pre.CATEGORIES))

    def test_all_remote_requests_are_read_only_queries_and_no_build_reads(self):
        self.run_report()
        for call in self.cloud.calls:
            if call[0] in {"GET", "POST"}:
                review_read.validate_request(call[0], call[1], call[2])
                if call[0] == "POST":
                    self.assertTrue(call[1].endswith((":testIamPermissions", ":getIamPolicy")))
            self.assertNotIn("cloudbuild.googleapis.com/v1", str(call))
            self.assertNotIn("builds submit", str(call))

    def test_failed_request_is_cached_without_a_second_attempt(self):
        reader = pre.CloudReader()
        with patch.object(reader, "local", return_value="fake-token"):
            with patch.object(
                review_read,
                "request",
                side_effect=review_read.ReadFailure(403, "PERMISSION_DENIED"),
            ) as remote:
                for _ in range(2):
                    with self.assertRaises(review_read.ReadFailure):
                        reader.api("GET", pre.CRM + f"/v3/projects/{pre.PROJECT_NUMBER}")
        self.assertEqual(remote.call_count, 1)

    def test_real_github_adapter_uses_get_and_redacts_failed_stderr(self):
        reader = pre.CloudReader()
        with patch("subprocess.run") as command:
            command.return_value.returncode = 1
            command.return_value.stderr = self.cloud.private + " (HTTP 403)"
            with self.assertRaises(pre.ProbeFailure) as captured:
                reader.github("user")
        self.assertNotIn(self.cloud.private, str(captured.exception))
        self.assertEqual(command.call_args.args[0][:4], ["gh", "api", "--method", "GET"])

    def test_cli_adapter_cannot_login_modify_or_run_workloads(self):
        reader = pre.CloudReader()
        with patch("subprocess.run") as command:
            for args in (
                ("auth", "login"),
                ("config", "set", "project", pre.PROJECT),
                ("run", "jobs", "execute", pre.JOB),
                ("services", "enable", "run.googleapis.com"),
            ):
                with self.assertRaises(pre.ProbeFailure):
                    reader.local(*args)
            with self.assertRaises(pre.ProbeFailure):
                reader.github("repos/" + pre.REPO + "/actions/workflows/gcp-deploy.yml/dispatches")
        command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
