"""Owner setup regression tests: all cloud/network/process calls are mocked."""

import contextlib
import copy
import io
import json
import unittest
import urllib.error
from unittest.mock import Mock, patch

import bootstrap

RAW_API_GET = bootstrap.api_get


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.plan = bootstrap.identity_plan()
        self.cfg = {
            "enabled": True,
            "image_digest": "sha256:" + "a" * 64,
            "replacement_build_id": "11111111-2222-4333-8444-555555555555",
            "data_bucket": f"synthetic-quality-{bootstrap.PROJECT_NUMBER}-data-as1",
            "run_id": "smoke-001",
        }
        self.specs = bootstrap.resource_specs(self.plan)
        self.resources = {url: self.actual(kind, expected) for kind, url, expected, _ in self.specs}
        self.policies = {}
        self.calls = []
        self.api_calls = []
        self.job = None
        self.keys = []
        self.extra_providers = []
        self.missing_apis = set()
        self.registry_digest = self.cfg["image_digest"]
        self.build = {
            "id": self.cfg["replacement_build_id"],
            "projectId": bootstrap.PROJECT,
            "status": "SUCCESS",
            "steps": [{"name": "gcr.io/cloud-builders/docker", "status": "SUCCESS"}],
            "substitutions": {"_TAG": bootstrap.APP_REV + "-recovery1"},
            "results": {
                "images": [
                    {
                        "digest": self.cfg["image_digest"],
                        "name": (
                            f"{bootstrap.REGION}-docker.pkg.dev/{bootstrap.PROJECT}/"
                            f"{bootstrap.REPOSITORY}/synthetic-quality:"
                            f"{bootstrap.APP_REV}-recovery1"
                        ),
                    }
                ]
            },
        }
        for binding in self.plan["bindings"]:
            if binding["scope"] == "job":
                continue
            policy = self.policy(binding)
            policy["bindings"].append({"role": binding["role"], "members": [binding["member"]]})
        # Unrelated existing administrative/read bindings must survive additions.
        project = next(b for b in self.plan["bindings"] if b["scope"] == "project")
        self.unrelated = {"role": "roles/viewer", "members": ["user:existing@example.org"]}
        self.policy(project)["bindings"].append(copy.deepcopy(self.unrelated))
        self.gc_mock = self.stack.enter_context(
            patch.object(bootstrap, "gc", side_effect=self.fake_gc)
        )
        self.api_mock = self.stack.enter_context(
            patch.object(bootstrap, "api_get", side_effect=self.fake_api)
        )
        self.owner_mock = self.stack.enter_context(
            patch.object(bootstrap, "owner_context", side_effect=lambda: set(self.missing_apis))
        )
        self.load_mock = self.stack.enter_context(
            patch.object(
                bootstrap, "load_config", side_effect=lambda **kwargs: copy.deepcopy(self.cfg)
            )
        )
        self.validate_job_mock = self.stack.enter_context(patch.object(bootstrap, "validate_job"))
        self.stack.enter_context(
            patch("subprocess.run", side_effect=AssertionError("Real process prohibited"))
        )
        self.stack.enter_context(
            patch(
                "bootstrap.urllib.request.urlopen",
                side_effect=AssertionError("Real network prohibited"),
            )
        )
        self.stack.enter_context(
            patch(
                "bootstrap.urllib.request.build_opener",
                side_effect=AssertionError("Real network prohibited"),
            )
        )
        self.stack.enter_context(
            patch("socket.socket", side_effect=AssertionError("Network sockets prohibited"))
        )

    def actual(self, kind, expected):
        if kind == "role":
            return {
                "name": expected["name"],
                "stage": "GA",
                "includedPermissions": list(expected["permissions"]),
            }
        if kind == "service_account":
            return {"email": expected["email"], "projectId": bootstrap.PROJECT, "disabled": False}
        if kind == "pool":
            return {"name": expected["name"], "state": "ACTIVE", "disabled": False}
        return {
            "name": expected["name"],
            "state": "ACTIVE",
            "disabled": False,
            "attributeMapping": copy.deepcopy(expected["attributeMapping"]),
            "attributeCondition": expected["attributeCondition"],
            "oidc": {
                "issuerUri": expected["issuerUri"],
                "allowedAudiences": expected["allowedAudiences"],
            },
        }

    def policy(self, binding):
        key = tuple(bootstrap.policy_command(binding))
        return self.policies.setdefault(key, {"bindings": []})

    def fake_api(self, url, missing=False):
        self.api_calls.append((url, missing))
        if "/keys?keyTypes=USER_MANAGED" in url:
            return {"keys": copy.deepcopy(self.keys)}
        if url.endswith("/providers"):
            providers = [
                copy.deepcopy(d) for key, d in self.resources.items() if key.startswith(url + "/")
            ]
            return {
                "workloadIdentityPoolProviders": providers + copy.deepcopy(self.extra_providers)
            }
        if url.startswith("https://run.googleapis.com/"):
            return copy.deepcopy(self.job)
        if url.endswith("/serviceAccounts/" + bootstrap.RUNTIME_SA):
            return self.actual("service_account", {"email": bootstrap.RUNTIME_SA})
        if url in self.resources:
            return copy.deepcopy(self.resources[url])
        if missing:
            return None
        raise AssertionError("Unexpected API read: " + url)

    def fake_gc(self, *args, **kwargs):
        self.calls.append(args)
        if args[:2] == ("builds", "describe"):
            return copy.deepcopy(self.build)
        if args[:4] == ("artifacts", "docker", "images", "describe"):
            return {"image_summary": {"digest": self.registry_digest}}
        if "get-iam-policy" in args:
            return copy.deepcopy(self.policies.setdefault(args, {"bindings": []}))
        if args[:2] == ("services", "enable"):
            self.assertLessEqual(set(args[2:]), bootstrap.ENABLE_APIS)
            self.missing_apis.difference_update(args[2:])
            return "enabled"
        for kind, url, expected, command in self.specs:
            if args == tuple(command):
                self.assertNotIn(url, self.resources)
                self.resources[url] = self.actual(kind, expected)
                return "created"
        for binding in self.plan["bindings"]:
            if args == tuple(bootstrap.policy_command(binding, add=True)):
                self.policy(binding)["bindings"].append(
                    {"role": binding["role"], "members": [binding["member"]]}
                )
                return "binding added"
        raise AssertionError("Unexpected gcloud command: " + repr(args))

    def run_main(self, action, approve=None):
        arguments = ["bootstrap.py", action]
        if approve is not None:
            arguments += ["--approve", approve]
        with patch("sys.argv", arguments):
            bootstrap.main()

    def writes(self):
        return [
            args
            for args in self.calls
            if "create" in args
            or "create-oidc" in args
            or "add-iam-policy-binding" in args
            or args[:2] == ("services", "enable")
        ]

    def test_plan_is_offline(self):
        self.run_main("plan")
        self.gc_mock.assert_not_called()
        self.api_mock.assert_not_called()
        self.owner_mock.assert_not_called()
        self.load_mock.assert_not_called()

    def test_check_matching_state_never_writes(self):
        self.run_main("check")
        self.assertFalse(self.writes())
        self.assertTrue(self.validate_job_mock.call_count == 0)

    def test_disabled_configuration_can_run_read_only_check_without_enabling(self):
        self.cfg["enabled"] = False
        self.run_main("check")
        self.assertFalse(self.writes())

    def test_check_missing_resources_and_apis_never_writes(self):
        self.resources = {}
        self.policies = {}
        self.missing_apis = set(bootstrap.ENABLE_APIS)
        self.run_main("check")
        self.assertFalse(self.writes())
        self.assertFalse(self.resources)

    def test_apply_requires_exact_new_authorization(self):
        for phrase in (None, "YES", "WIF-SETUP-WRONG-PROJECT"):
            with self.subTest(phrase=phrase):
                with self.assertRaisesRegex(RuntimeError, "approval"):
                    self.run_main("apply", phrase)
        self.assertFalse(self.calls)
        self.owner_mock.assert_not_called()

    def test_disabled_config_blocks_before_any_write(self):
        self.cfg["enabled"] = False
        with self.assertRaisesRegex(RuntimeError, "disabled/incomplete"):
            self.run_main("apply", bootstrap.APPROVAL)
        self.assertFalse(self.writes())

    def test_matching_apply_is_idempotent_noop(self):
        initial = copy.deepcopy(self.policies)
        self.run_main("apply", bootstrap.APPROVAL)
        self.run_main("apply", bootstrap.APPROVAL)
        self.assertFalse(self.writes())
        self.assertEqual(self.policies, initial)

    def test_missing_resources_created_once_and_iam_is_additive(self):
        self.resources = {}
        self.policies = {}
        project = next(b for b in self.plan["bindings"] if b["scope"] == "project")
        self.policy(project)["bindings"].append(copy.deepcopy(self.unrelated))
        self.missing_apis = set(bootstrap.ENABLE_APIS)
        self.run_main("apply", bootstrap.APPROVAL)
        writes = self.writes()
        self.assertEqual(sum("create" in a or "create-oidc" in a for a in writes), 8)
        self.assertEqual(sum("add-iam-policy-binding" in a for a in writes), 5)
        self.assertEqual(sum(a[:2] == ("services", "enable") for a in writes), 1)
        self.assertIn(self.unrelated, self.policy(project)["bindings"])
        count = len(writes)
        self.run_main("apply", bootstrap.APPROVAL)
        self.assertEqual(len(self.writes()), count)
        self.assertFalse(
            any("delete" in a or "update" in a or "set-iam-policy" in a for a in self.calls)
        )

    def test_absent_job_defers_executor_without_project_execute(self):
        self.policies = {}
        self.run_main("apply", bootstrap.APPROVAL)
        self.assertFalse(
            any(a[:3] == ("run", "jobs", "add-iam-policy-binding") for a in self.writes())
        )
        for binding in self.plan["bindings"]:
            if binding["member"] == "serviceAccount:" + bootstrap.EXECUTE_SA:
                self.assertEqual(binding["scope"], "job")

    def test_existing_job_gets_only_exact_job_executor_grant(self):
        self.job = {"name": "approved-job-fixture"}
        self.run_main("apply", bootstrap.APPROVAL)
        adds = [a for a in self.writes() if "add-iam-policy-binding" in a]
        self.assertEqual(len(adds), 1)
        self.assertEqual(adds[0][:4], ("run", "jobs", "add-iam-policy-binding", bootstrap.JOB))
        self.validate_job_mock.assert_called_once_with(self.job, self.cfg)

    def test_job_configuration_failure_blocks_all_setup_writes(self):
        self.job = {"name": "unexpected-job"}
        self.validate_job_mock.side_effect = RuntimeError("job digest mismatch")
        with self.assertRaisesRegex(RuntimeError, "digest"):
            self.run_main("apply", bootstrap.APPROVAL)
        self.assertFalse(self.writes())

    def test_existing_provider_trust_drift_stops_without_repair(self):
        provider = next(url for kind, url, _, _ in self.specs if kind == "provider")
        self.resources[provider]["attributeCondition"] = "true"
        with self.assertRaisesRegex(RuntimeError, "trust/audience"):
            self.run_main("apply", bootstrap.APPROVAL)
        self.assertFalse(self.writes())

    def test_existing_pool_extra_provider_blocks_principalset_crossover(self):
        self.extra_providers = [{"name": self.plan["pools"][0]["name"] + "/providers/unreviewed"}]
        with self.assertRaisesRegex(RuntimeError, "Unexpected provider"):
            self.run_main("apply", bootstrap.APPROVAL)
        self.assertFalse(self.writes())

    def test_existing_user_managed_key_stops_without_deletion(self):
        self.keys = [{"name": "existing-key-metadata-only"}]
        with self.assertRaisesRegex(RuntimeError, "user-managed keys"):
            self.run_main("apply", bootstrap.APPROVAL)
        self.assertFalse(self.writes())

    def test_role_with_extra_permission_is_never_repaired(self):
        role = next(url for kind, url, _, _ in self.specs if kind == "role")
        self.resources[role]["includedPermissions"].append("run.jobs.run")
        with self.assertRaisesRegex(RuntimeError, "custom role differs"):
            self.run_main("apply", bootstrap.APPROVAL)
        self.assertFalse(self.writes())

    def test_executor_project_grant_is_rejected(self):
        binding = next(b for b in self.plan["bindings"] if b["scope"] == "project")
        self.policy(binding)["bindings"].append(
            {"role": "roles/run.invoker", "members": ["serviceAccount:" + bootstrap.EXECUTE_SA]}
        )
        with self.assertRaisesRegex(RuntimeError, "unexpected project grant"):
            self.run_main("apply", bootstrap.APPROVAL)
        self.assertFalse(self.writes())

    def test_project_pool_principalset_cannot_bypass_service_account_trust(self):
        base = copy.deepcopy(self.policies)
        project = next(b for b in self.plan["bindings"] if b["scope"] == "project")
        pool = self.plan["pools"][0]["name"]
        member = f"principalSet://iam.googleapis.com/{pool}/attribute.repository_id/" + str(
            bootstrap.REPO_ID
        )
        for role in (
            "roles/iam.workloadIdentityUser",
            "roles/iam.serviceAccountTokenCreator",
            "roles/editor",
        ):
            with self.subTest(role=role):
                self.policies = copy.deepcopy(base)
                self.policy(project)["bindings"].append({"role": role, "members": [member]})
                before = copy.deepcopy(self.policies)
                with self.assertRaises(RuntimeError):
                    self.run_main("apply", bootstrap.APPROVAL)
                self.assertFalse(self.writes())
                self.assertEqual(self.policies, before)

    def test_project_pool_wildcard_and_subject_grants_are_rejected(self):
        base = copy.deepcopy(self.policies)
        project = next(b for b in self.plan["bindings"] if b["scope"] == "project")
        for pool in self.plan["pools"]:
            for member in (
                f"principalSet://iam.googleapis.com/{pool['name']}/*",
                f"principal://iam.googleapis.com/{pool['name']}/subject/fixture-subject",
            ):
                with self.subTest(member=member):
                    self.policies = copy.deepcopy(base)
                    self.policy(project)["bindings"].append(
                        {"role": "roles/iam.serviceAccountTokenCreator", "members": [member]}
                    )
                    before = copy.deepcopy(self.policies)
                    with self.assertRaises(RuntimeError):
                        self.run_main("apply", bootstrap.APPROVAL)
                    self.assertFalse(self.writes())
                    self.assertEqual(self.policies, before)

    def test_dedicated_identity_token_creator_trust_is_rejected(self):
        binding = self.plan["bindings"][0]
        self.policy(binding)["bindings"].append(
            {"role": "roles/iam.serviceAccountTokenCreator", "members": ["user:other@example.org"]}
        )
        with self.assertRaisesRegex(RuntimeError, "unexpected impersonation trust"):
            self.run_main("apply", bootstrap.APPROVAL)
        self.assertFalse(self.writes())

    def test_conditional_existing_grant_is_not_broadened(self):
        binding = self.plan["bindings"][0]
        policy = {
            "bindings": [
                {
                    "role": binding["role"],
                    "members": [binding["member"]],
                    "condition": {"expression": "false"},
                }
            ]
        }
        with self.assertRaisesRegex(RuntimeError, "conditionally"):
            bootstrap.binding_present(policy, binding)

    def test_image_failure_or_cancellation_blocks_writes(self):
        for status in ("CANCELLED", "FAILURE", "WORKING"):
            with self.subTest(status=status):
                self.build["status"] = status
                with self.assertRaisesRegex(RuntimeError, "successful project build"):
                    self.run_main("apply", bootstrap.APPROVAL)
        self.assertFalse(self.writes())

    def test_cancelled_original_id_cannot_authorize_setup(self):
        self.cfg["replacement_build_id"] = "6f75c5dd-cc3a-4d47-9c9d-43f51fa3c4ba"
        with self.assertRaisesRegex(RuntimeError, "Cancelled build"):
            self.run_main("apply", bootstrap.APPROVAL)
        self.assertFalse(self.calls)

    def test_registry_digest_mismatch_blocks_writes(self):
        self.registry_digest = "sha256:" + "b" * 64
        with self.assertRaisesRegex(RuntimeError, "immutable digest"):
            self.run_main("apply", bootstrap.APPROVAL)
        self.assertFalse(self.writes())

    def test_setup_never_requests_keys_builds_workloads_or_deletions(self):
        self.resources = {}
        self.policies = {}
        self.run_main("apply", bootstrap.APPROVAL)
        self.assertFalse(
            any(
                a[:2] == ("builds", "submit")
                or a[:3]
                in {
                    ("run", "jobs", "create"),
                    ("run", "jobs", "execute"),
                    ("iam", "service-accounts", "keys"),
                }
                for a in self.calls
            )
        )
        self.assertTrue(all("delete" not in a and "set-iam-policy" not in a for a in self.calls))

    def test_raw_api_403_is_never_missing(self):
        body = io.BytesIO(json.dumps({"error": {"status": "PERMISSION_DENIED"}}).encode())
        error = urllib.error.HTTPError(
            "https://iam.googleapis.com/v1/example", 403, "denied", {}, body
        )
        opener = Mock()
        opener.open.side_effect = error
        with (
            patch.object(bootstrap, "gc", return_value="unit-test-placeholder"),
            patch("bootstrap.urllib.request.build_opener", return_value=opener),
        ):
            with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
                RAW_API_GET("https://iam.googleapis.com/v1/example", missing=True)

    def test_raw_api_structured_404_can_be_missing(self):
        body = io.BytesIO(json.dumps({"error": {"status": "NOT_FOUND"}}).encode())
        error = urllib.error.HTTPError(
            "https://iam.googleapis.com/v1/example", 404, "missing", {}, body
        )
        opener = Mock()
        opener.open.side_effect = error
        with (
            patch.object(bootstrap, "gc", return_value="unit-test-placeholder"),
            patch("bootstrap.urllib.request.build_opener", return_value=opener),
        ):
            self.assertIsNone(RAW_API_GET("https://iam.googleapis.com/v1/example", missing=True))


if __name__ == "__main__":
    unittest.main()
