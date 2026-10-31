"""Offline trust, least-privilege, fail-closed and job-control regressions."""

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bootstrap
import job_control as control
import settings


def configured():
    return {
        "enabled": True,
        "approval_mode": "independent",
        "solo_owner_approval_authorized": False,
        "image_digest": "sha256:" + "a" * 64,
        "replacement_build_id": "11111111-2222-4333-8444-555555555555",
        "environment_bypass_disabled_attested": {"gcp-deploy": True, "gcp-execute": True},
        "data_bucket": "synthetic-quality-984592102391-data-as1",
        "run_id": "smoke-001",
    }


def ready_job(cfg):
    return {
        "name": settings.job_name(),
        "uid": "reviewed-job-uid",
        "etag": "reviewed-etag",
        "terminalCondition": {"state": "CONDITION_SUCCEEDED"},
        **control.deployment_body(cfg),
    }


class FakeApi:
    def __init__(self, cfg, job=None):
        self.cfg = cfg
        self.job = copy.deepcopy(job)
        self.calls = []
        self.executions = []
        self.post_error = None
        self.image = {"uri": settings.image_uri(cfg)}

    def request(self, method, url, body=None, missing=False):
        self.calls.append((method, url, copy.deepcopy(body)))
        if method == "POST" and self.post_error:
            raise self.post_error
        if method == "GET" and url.startswith("https://artifactregistry.googleapis.com/"):
            return self.image
        if method == "GET" and url == settings.job_url():
            return copy.deepcopy(self.job)
        if method == "GET" and "/executions?" in url:
            return {"executions": self.executions}
        if method == "POST" and "?jobId=" in url:
            self.job = ready_job(self.cfg)
            return {"name": "projects/p/locations/r/operations/create"}
        if method == "POST" and url.endswith(":run"):
            return {
                "name": "projects/p/locations/r/operations/run",
                "metadata": {"name": settings.job_name() + "/executions/one"},
            }
        raise AssertionError("Unexpected fake API request")

    def posts(self):
        return [x for x in self.calls if x[0] == "POST"]


class OidcTests(unittest.TestCase):
    def setUp(self):
        self.cfg = configured()
        self.env = {
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_REPOSITORY_ID": settings.REPO_ID,
            "GITHUB_REPOSITORY_OWNER_ID": settings.OWNER_ID,
            "GITHUB_ACTOR_ID": settings.OWNER_ID,
            "GITHUB_RUN_ATTEMPT": "1",
            "CONFIRM_DIGEST": self.cfg["image_digest"],
            "CONFIRM_ACTION": "true",
            "CONFIRM_JOB_UID": "reviewed-job-uid",
        }
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(
            patch("urllib.request.urlopen", side_effect=AssertionError("Network prohibited"))
        )
        self.stack.enter_context(
            patch("urllib.request.build_opener", side_effect=AssertionError("Network prohibited"))
        )
        self.stack.enter_context(
            patch("subprocess.run", side_effect=AssertionError("Subprocess prohibited"))
        )

    def test_default_configuration_is_disabled(self):
        config = settings.load_config()
        self.assertFalse(config["enabled"])
        self.assertEqual(config["approval_mode"], "independent")
        self.assertFalse(config["solo_owner_approval_authorized"])
        self.assertRegex(config["image_digest"], r"^sha256:[0-9a-f]{64}$")
        self.assertNotEqual(config["replacement_build_id"], settings.KNOWN_CANCELLED)
        self.assertEqual(
            config["environment_bypass_disabled_attested"],
            {"gcp-deploy": False, "gcp-execute": False},
        )
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            settings.load_config(require_enabled=True)

    def test_missing_or_tag_image_is_rejected(self):
        for value in (None, "latest", "sha256:bad", "image:tag"):
            cfg = {**self.cfg, "image_digest": value}
            with tempfile.TemporaryDirectory() as folder:
                p = Path(folder) / "config.json"
                p.write_text(json.dumps(cfg))
                with self.assertRaisesRegex(RuntimeError, "image_digest"):
                    settings.load_config(True, p)

    def test_solo_policy_requires_explicit_boolean_approval_before_activation(self):
        for authorized in (False, None, "true", 1):
            cfg = {
                **self.cfg,
                "approval_mode": "solo-owner",
                "solo_owner_approval_authorized": authorized,
            }
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "config.json"
                path.write_text(json.dumps(cfg))
                with self.assertRaises(RuntimeError):
                    settings.load_config(True, path)
        settings.validate_approval_policy("solo-owner", True)
        with self.assertRaises(RuntimeError):
            settings.validate_approval_policy("independent", True)

    def test_cancelled_build_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / "config.json"
            p.write_text(json.dumps({**self.cfg, "replacement_build_id": settings.KNOWN_CANCELLED}))
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                settings.load_config(True, p)

    def test_activation_requires_both_explicit_owner_bypass_attestations(self):
        for environment in ("gcp-deploy", "gcp-execute"):
            cfg = copy.deepcopy(self.cfg)
            cfg["environment_bypass_disabled_attested"][environment] = False
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "config.json"
                path.write_text(json.dumps(cfg))
                with self.assertRaisesRegex(RuntimeError, "Owner must review"):
                    settings.load_config(True, path)

    def test_protected_bucket_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / "config.json"
            p.write_text(json.dumps({**self.cfg, "data_bucket": "gcp-idq-data-984592102391"}))
            with self.assertRaisesRegex(RuntimeError, "protected"):
                settings.load_config(True, p)

    def test_wrong_dispatch_identity_event_ref_and_confirmation_are_rejected(self):
        for key, value in (
            ("GITHUB_EVENT_NAME", "pull_request"),
            ("GITHUB_REF", "refs/heads/feature"),
            ("GITHUB_REPOSITORY_ID", "1"),
            ("GITHUB_REPOSITORY_OWNER_ID", "2"),
            ("GITHUB_ACTOR_ID", "2"),
            ("GITHUB_RUN_ATTEMPT", "2"),
            ("CONFIRM_ACTION", "false"),
            ("CONFIRM_DIGEST", "sha256:" + "b" * 64),
        ):
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                control.verify_dispatch({**self.env, key: value}, "deploy", self.cfg)

    def test_manual_main_dispatch_is_accepted(self):
        control.verify_dispatch(self.env, "deploy", self.cfg)

    def test_execution_needs_reviewed_uid(self):
        with self.assertRaisesRegex(RuntimeError, "UID"):
            control.verify_dispatch({**self.env, "CONFIRM_JOB_UID": ""}, "execute", self.cfg)

    def test_pools_and_identity_grants_are_separate(self):
        plan = bootstrap.identity_plan()
        self.assertEqual(len(plan["pools"]), 2)
        self.assertNotEqual(plan["pools"][0]["name"], plan["pools"][1]["name"])
        for pool in plan["pools"]:
            grants = [b for b in plan["bindings"] if b["resource"] == pool["service_account"]]
            self.assertEqual(len(grants), 1)
            self.assertIn(
                pool["name"] + "/attribute.repository_id/" + settings.REPO_ID, grants[0]["member"]
            )

    def test_trust_restricts_numeric_ids_main_manual_workflow_and_environment(self):
        for action, pool in zip(
            ("deploy", "execute"), bootstrap.identity_plan()["pools"], strict=True
        ):
            provider = pool["provider"]
            condition = provider["attributeCondition"]
            for clause in (
                f"attribute.repository_id == '{settings.REPO_ID}'",
                f"attribute.repository_owner_id == '{settings.OWNER_ID}'",
                "attribute.ref == 'refs/heads/main'",
                "attribute.ref_type == 'branch'",
                "attribute.event_name == 'workflow_dispatch'",
                f"attribute.environment == 'gcp-{action}'",
                f"attribute.workflow_ref == '{settings.REPO}/.github/workflows/"
                f"gcp-{action}.yml@refs/heads/main'",
            ):
                self.assertIn(clause, condition)
            self.assertNotIn(" || ", condition)
            self.assertEqual(
                provider["allowedAudiences"], ["https://iam.googleapis.com/" + provider["name"]]
            )

    def test_roles_cannot_build_override_update_delete_or_cross_execute(self):
        plan = bootstrap.identity_plan()
        deploy, execute = plan["roles"]
        self.assertEqual(set(deploy["permissions"]), {"run.jobs.create", "run.jobs.get"})
        self.assertEqual(
            set(execute["permissions"]),
            {"run.jobs.run", "run.jobs.get", "run.executions.get", "run.executions.list"},
        )
        executor_grants = [
            b for b in plan["bindings"] if b["member"] == "serviceAccount:" + settings.EXECUTE_SA
        ]
        self.assertEqual([b["scope"] for b in executor_grants], ["job"])
        self.assertFalse(
            any(
                "cloudbuild" in p or "setIamPolicy" in p
                for r in plan["roles"]
                for p in r["permissions"]
            )
        )

    def test_matching_provider_is_idempotently_validated(self):
        p = bootstrap.identity_plan()["pools"][0]["provider"]
        actual = {
            "name": p["name"],
            "state": "ACTIVE",
            "attributeMapping": p["attributeMapping"],
            "attributeCondition": p["attributeCondition"],
            "oidc": {"issuerUri": p["issuerUri"], "allowedAudiences": p["allowedAudiences"]},
        }
        bootstrap.validate_resource("provider", actual, p)
        actual["attributeCondition"] = "true"
        with self.assertRaisesRegex(RuntimeError, "trust"):
            bootstrap.validate_resource("provider", actual, p)

    def test_existing_conditional_grant_is_not_broadened(self):
        b = bootstrap.identity_plan()["bindings"][0]
        policy = {
            "bindings": [
                {"role": b["role"], "members": [b["member"]], "condition": {"expression": "false"}}
            ]
        }
        with self.assertRaisesRegex(RuntimeError, "conditionally"):
            bootstrap.binding_present(policy, b)

    def test_existing_unrelated_binding_is_not_replaced(self):
        b = bootstrap.identity_plan()["bindings"][0]
        policy = {"bindings": [{"role": "roles/viewer", "members": ["user:reviewer@example.org"]}]}
        before = copy.deepcopy(policy)
        self.assertFalse(bootstrap.binding_present(policy, b))
        self.assertEqual(policy, before)

    def test_owner_plan_makes_no_cloud_calls(self):
        with patch("sys.argv", ["bootstrap.py", "plan"]):
            bootstrap.main()

    def test_owner_apply_disabled_stops_before_cloud_access(self):
        with patch("sys.argv", ["bootstrap.py", "apply", "--approve", bootstrap.APPROVAL]):
            with self.assertRaisesRegex(RuntimeError, "disabled"):
                bootstrap.main()

    def test_new_job_uses_digest_and_never_executes(self):
        client = FakeApi(self.cfg)
        result = control.deploy(client, self.cfg, sleeper=lambda _: None)
        self.assertIn("NOT executed", result["status"])
        self.assertEqual(len(client.posts()), 1)
        self.assertIn("?jobId=", client.posts()[0][1])
        self.assertEqual(
            client.posts()[0][2]["template"]["template"]["containers"][0]["image"],
            settings.image_uri(self.cfg),
        )

    def test_existing_matching_job_is_never_updated(self):
        client = FakeApi(self.cfg, ready_job(self.cfg))
        control.deploy(client, self.cfg)
        self.assertFalse(client.posts())

    def test_unmarked_existing_job_is_not_adopted(self):
        job = ready_job(self.cfg)
        job["labels"] = {}
        client = FakeApi(self.cfg, job)
        with self.assertRaisesRegex(RuntimeError, "adopt"):
            control.deploy(client, self.cfg)
        self.assertFalse(client.posts())

    def test_existing_wrong_digest_is_not_updated(self):
        job = ready_job(self.cfg)
        job["template"]["template"]["containers"][0]["image"] += "bad"
        client = FakeApi(self.cfg, job)
        with self.assertRaisesRegex(RuntimeError, "image"):
            control.deploy(client, self.cfg)
        self.assertFalse(client.posts())

    def test_registry_mismatch_prevents_deployment(self):
        client = FakeApi(self.cfg)
        client.image = {"uri": "wrong"}
        with self.assertRaisesRegex(RuntimeError, "Registry"):
            control.deploy(client, self.cfg)
        self.assertFalse(client.posts())

    def test_lost_create_ack_is_not_retried(self):
        client = FakeApi(self.cfg)
        client.post_error = RuntimeError("Lost acknowledgement")
        with self.assertRaisesRegex(RuntimeError, "Lost"):
            control.deploy(client, self.cfg)
        self.assertEqual(len(client.posts()), 1)

    def test_execute_posts_once_without_overrides_and_returns_async(self):
        client = FakeApi(self.cfg, ready_job(self.cfg))
        result = control.execute(client, self.cfg, "reviewed-job-uid")
        self.assertIn("REQUESTED", result["status"])
        self.assertEqual(len(client.posts()), 1)
        self.assertEqual(client.posts()[0][2], {"etag": "reviewed-etag"})

    def test_execute_rejects_changed_uid(self):
        client = FakeApi(self.cfg, ready_job(self.cfg))
        with self.assertRaisesRegex(RuntimeError, "UID"):
            control.execute(client, self.cfg, "different")
        self.assertFalse(client.posts())

    def test_duplicate_execution_history_blocks_another_request(self):
        for field in ("counter", "list"):
            client = FakeApi(self.cfg, ready_job(self.cfg))
            if field == "counter":
                client.job["executionCount"] = 1
            else:
                client.executions = [{"name": "existing"}]
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(RuntimeError, "[Hh]istory|inventory"),
            ):
                control.execute(client, self.cfg, "reviewed-job-uid")
            self.assertFalse(client.posts())

    def test_api_method_allowlist_rejects_iam_and_delete(self):
        client = control.Api("fake-offline-token")
        with self.assertRaisesRegex(RuntimeError, "allowlist"):
            client.request("DELETE", settings.job_url())
        with self.assertRaisesRegex(RuntimeError, "allowlist"):
            client.request("POST", "https://iam.googleapis.com/v1/example")

    def test_no_long_lived_token_or_sdk_dependency_is_required(self):
        with self.assertRaisesRegex(RuntimeError, "token"):
            control.Api("")


if __name__ == "__main__":
    unittest.main()
