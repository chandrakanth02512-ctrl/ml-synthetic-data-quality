"""Offline regression tests: every cloud/Git/network entry point is mocked.

Receipt operations use a temporary HOME, never the operator's deployment receipts.
"""

import contextlib
import copy
import datetime as dt
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, patch

import preflight
import stage2
import stage2_recovery as recovery
import stage3


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.home = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(os.environ, {"HOME": str(self.home)}))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.c = SimpleNamespace(
            account="operator@example.org",
            number="123456789012",
            data="synthetic-quality-123456789012-data-as1",
            source="synthetic-quality-123456789012-source-as1",
            repository="synthetic-quality-phase1",
            job="synthetic-quality-phase1-smoke",
            build="synthetic-quality-build@gcp-intelligent-data-quality.iam.gserviceaccount.com",
            runtime="synthetic-quality-runtime@gcp-intelligent-data-quality.iam.gserviceaccount.com",
        )
        self.uri = recovery.image_uri(self.c)
        self.digest = "sha256:" + "a" * 64
        self.replacement_id = "11111111-2222-4333-8444-555555555555"
        self.old = self.build(recovery.KNOWN_CANCELLED, "CANCELLED", recovery.REV)
        self.extra = None
        self.images = []
        self.jobs = []
        self.job = None
        self.commands = []
        self.api_calls = []
        self.page_calls = []
        self.submit_result = None
        self.submit_error = None
        recovery.state_root().mkdir(parents=True)
        self.original_bytes = (
            json.dumps(
                {"project": recovery.PROJECT, "revision": recovery.REV, "action": "build"},
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
        recovery.original_path().write_bytes(self.original_bytes)
        self.original_names = {p.name for p in recovery.state_root().iterdir()}
        self.stack.enter_context(patch.object(recovery, "gc", side_effect=self.fake_gc))
        self.stack.enter_context(patch.object(recovery, "api", side_effect=self.fake_api))
        self.stack.enter_context(patch.object(recovery, "pages", side_effect=self.fake_pages))
        self.git_mock = self.stack.enter_context(
            patch.object(recovery, "git", side_effect=self.fake_git)
        )
        self.stack.enter_context(patch.object(recovery, "Context", return_value=self.c))
        self.reviewed = self.stack.enter_context(patch.object(recovery, "reviewed_environment"))
        self.operator = self.stack.enter_context(patch.object(recovery, "operator_permissions"))
        # These blocks catch accidental calls through imported legacy helpers.
        for module, name in (
            (preflight, "gc"),
            (preflight, "api"),
            (stage2, "gc"),
            (stage2, "git"),
            (stage3, "gc"),
            (stage3, "api"),
            (stage3, "pages"),
        ):
            self.stack.enter_context(
                patch.object(
                    module, name, side_effect=AssertionError("Real cloud/Git call prohibited")
                )
            )
        self.stack.enter_context(
            patch(
                "preflight.urllib.request.urlopen", side_effect=AssertionError("Network prohibited")
            )
        )
        self.stack.enter_context(
            patch("subprocess.run", side_effect=AssertionError("Real subprocess prohibited"))
        )
        self.addCleanup(self.assert_original_unchanged)

    def assert_original_unchanged(self):
        self.assertEqual(recovery.original_path().read_bytes(), self.original_bytes)

    def build(self, build_id, status, tag):
        b = {
            "id": build_id,
            "projectId": recovery.PROJECT,
            "name": f"projects/{recovery.PROJECT}/locations/{recovery.REGION}/builds/{build_id}",
            "status": status,
            "createTime": "2026-10-30T10:01:00Z",
            "substitutions": {
                "_TAG": tag,
                "_REGION": recovery.REGION,
                "_REPOSITORY": self.c.repository,
                "_IMAGE": recovery.IMAGE,
            },
            "serviceAccount": f"projects/{recovery.PROJECT}/serviceAccounts/{self.c.build}",
            "options": {"logging": "CLOUD_LOGGING_ONLY", "machineType": "E2_STANDARD_2"},
            "timeout": "1200s",
            "source": {
                "storageSource": {
                    "bucket": self.c.source,
                    "object": "source/original.tgz"
                    if tag == recovery.REV
                    else f"source/recovery-{recovery.KNOWN_CANCELLED}/replacement.tgz",
                }
            },
            "images": [self.uri + ":" + tag],
            "steps": [{"name": "gcr.io/cloud-builders/docker", "status": status}],
            "results": {"images": []},
        }
        if status == "SUCCESS":
            b["results"]["images"] = [{"name": self.uri + ":" + tag, "digest": self.digest}]
        return b

    def marker(self):
        data = {
            "schema_version": 1,
            "project": recovery.PROJECT,
            "region": recovery.REGION,
            "revision": recovery.REV,
            "action": "one-replacement-build",
            "cancelled_build_id": recovery.KNOWN_CANCELLED,
            "replacement_tag": recovery.RECOVERY_TAG,
            "original_receipt_sha256": recovery.original_receipt()["sha256"],
            "operator": self.c.account,
            "created_at": "2026-10-30T10:00:00+00:00",
        }
        recovery.write_new_receipt(recovery.recovery_path(), data)
        return data

    def success(self):
        self.marker()
        self.extra = self.build(self.replacement_id, "SUCCESS", recovery.RECOVERY_TAG)
        self.images = [{"uri": self.uri + "@" + self.digest}]

    def fake_git(self, *args, **kwargs):
        if args[:1] == ("clone",):
            return ""
        if args == ("checkout", "--detach", recovery.REV):
            return ""
        if args[:2] == ("rev-parse", "HEAD"):
            return recovery.REV
        if args[:2] == ("status", "--porcelain"):
            return ""
        if args[:1] == ("ls-remote",):
            return recovery.REV + "\trefs/heads/main"
        raise AssertionError("Unexpected Git read: " + repr(args))

    def fake_gc(self, *args, **kwargs):
        self.commands.append(args)
        if args[:2] == ("builds", "describe"):
            if args[2] == recovery.KNOWN_CANCELLED:
                return copy.deepcopy(self.old)
            if self.extra is not None and args[2] == self.extra["id"]:
                return copy.deepcopy(self.extra)
            raise AssertionError("Unreviewed build describe")
        if args[:2] == ("builds", "list"):
            return [copy.deepcopy(self.old)] + (
                [] if self.extra is None else [copy.deepcopy(self.extra)]
            )
        if args[:4] == ("artifacts", "docker", "images", "describe"):
            return {"image_summary": {"digest": self.digest}}
        if args[:2] == ("meta", "list-files-for-upload"):
            return "Dockerfile\ncloudbuild.yaml\nsrc/synthetic_quality/cloud_job.py"
        if args[:2] == ("builds", "submit"):
            if self.submit_error:
                raise self.submit_error
            result = self.submit_result or {"id": self.replacement_id}
            self.extra = self.build(result["id"], "WORKING", recovery.RECOVERY_TAG)
            self.extra["createTime"] = dt.datetime.now(dt.UTC).isoformat()
            return copy.deepcopy(self.extra if self.submit_result is None else result)
        if args[:3] == ("run", "jobs", "create"):
            self.job = {
                "name": (
                    f"projects/{recovery.PROJECT}/locations/{recovery.REGION}/jobs/{self.c.job}"
                ),
                "labels": {"deployment": "synthetic-quality-phase1"},
                "terminalCondition": {"state": "CONDITION_SUCCEEDED"},
                "template": {
                    "taskCount": 1,
                    "parallelism": 1,
                    "template": {
                        "serviceAccount": self.c.runtime,
                        "timeout": "600s",
                        "maxRetries": 0,
                        "containers": [
                            {
                                "image": self.uri + "@" + self.digest,
                                "resources": {"limits": {"cpu": "1", "memory": "1Gi"}},
                                "env": [
                                    {"name": k, "value": v}
                                    for k, v in recovery.env_vars(self.c).items()
                                ],
                            }
                        ],
                    },
                },
            }
            self.jobs = [copy.deepcopy(self.job)]
            return "created"
        raise AssertionError("Unexpected command: " + repr(args))

    def fake_api(self, method, url, *args, **kwargs):
        self.api_calls.append((method, url))
        if method == "GET" and url == recovery.job_url(self.c):
            return copy.deepcopy(self.job)
        raise AssertionError("Unexpected API request: " + method + " " + url)

    def fake_pages(self, url, field):
        self.page_calls.append((url, field))
        if url == recovery.repo_url(self.c) + "/dockerImages" and field == "dockerImages":
            return copy.deepcopy(self.images)
        if field == "jobs":
            return copy.deepcopy(self.jobs)
        if field == "executions":
            return []
        raise AssertionError("Unexpected paginated read")

    def run_main(self, action):
        with patch("sys.argv", ["stage2.py", action]):
            recovery.main()

    def mutators(self):
        return [
            a
            for a in self.commands
            if a[:2] == ("builds", "submit") or a[:3] == ("run", "jobs", "create")
        ]

    def test_cancelled_build_is_recognized_not_success(self):
        snapshot = recovery.check_ready(self.c)
        self.assertEqual(snapshot["cancelled_build"]["id"], recovery.KNOWN_CANCELLED)
        self.assertIsNone(snapshot["replacement_build"])
        with self.assertRaises(RuntimeError):
            recovery.verify_success(self.c, snapshot)

    def test_exact_cancelled_id_required(self):
        self.old["id"] = self.replacement_id
        with self.assertRaisesRegex(RuntimeError, "Exact cancelled"):
            recovery.cancelled_build(self.c)

    def test_cancelled_build_and_step_status_required(self):
        for field in ("build", "step"):
            original = copy.deepcopy(self.old)
            if field == "build":
                self.old["status"] = "WORKING"
            else:
                self.old["steps"][0]["status"] = "SUCCESS"
            with self.assertRaisesRegex(RuntimeError, "both remain CANCELLED"):
                recovery.cancelled_build(self.c)
            self.old = original

    def test_cancelled_build_cannot_publish_image(self):
        self.old["results"]["images"] = [{"digest": self.digest}]
        with self.assertRaisesRegex(RuntimeError, "reports published images"):
            recovery.cancelled_build(self.c)

    def test_readonly_check_writes_no_receipts_or_mutations(self):
        self.run_main("recovery-check")
        self.assertEqual({p.name for p in recovery.state_root().iterdir()}, self.original_names)
        self.assertFalse(self.mutators())
        self.assertTrue(all(method == "GET" for method, _ in self.api_calls))
        self.reviewed.assert_called_once_with(self.c)
        self.operator.assert_called_once_with(self.c)

    def test_readonly_status_writes_no_receipts_or_mutations(self):
        self.run_main("recovery-status")
        self.assertEqual({p.name for p in recovery.state_root().iterdir()}, self.original_names)
        self.assertFalse(self.mutators())

    def test_existing_image_blocks_preflight(self):
        self.images = [{"uri": self.uri + "@" + self.digest}]
        with self.assertRaisesRegex(RuntimeError, "repository is not empty"):
            recovery.check_ready(self.c)

    def test_existing_job_blocks_preflight(self):
        self.job = {"name": "existing"}
        with self.assertRaisesRegex(RuntimeError, "Job already exists"):
            recovery.check_ready(self.c)

    def test_unrelated_regional_job_blocks_preflight(self):
        self.jobs = [{"name": "unrelated"}]
        with self.assertRaisesRegex(RuntimeError, "Job already exists"):
            recovery.check_ready(self.c)

    def test_permission_failure_is_not_job_absence(self):
        with patch.object(recovery, "api", side_effect=RuntimeError("PERMISSION DENIED")):
            with self.assertRaisesRegex(RuntimeError, "PERMISSION"):
                recovery.check_ready(self.c)

    def test_additional_build_without_receipt_not_adopted(self):
        self.extra = self.build(self.replacement_id, "SUCCESS", recovery.RECOVERY_TAG)
        with self.assertRaisesRegex(RuntimeError, "without this recovery receipt"):
            recovery.snapshot(self.c)

    def test_more_than_one_replacement_rejected(self):
        self.marker()
        extra = self.build(self.replacement_id, "SUCCESS", recovery.RECOVERY_TAG)
        other = self.build("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "FAILURE", recovery.RECOVERY_TAG)
        with patch.object(recovery, "gc", return_value=[self.old, extra, other]):
            with self.assertRaisesRegex(RuntimeError, "More than one additional"):
                recovery.relevant_builds(self.c)

    def test_duplicate_inventory_rejected(self):
        with patch.object(recovery, "gc", return_value=[self.old, self.old]):
            with self.assertRaisesRegex(RuntimeError, "duplicated"):
                recovery.relevant_builds(self.c)

    def test_missing_original_inventory_record_rejected(self):
        with patch.object(recovery, "gc", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "known cancelled build absent"):
                recovery.relevant_builds(self.c)

    def test_receipt_reservation_exclusive_and_original_preserved(self):
        self.marker()
        original_recovery = recovery.recovery_path().read_bytes()
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            recovery.write_new_receipt(recovery.recovery_path(), {"overwrite": "no"})
        self.assertEqual(recovery.recovery_path().read_bytes(), original_recovery)
        self.assert_original_unchanged()

    def test_reserved_no_build_interrupted_state_never_resubmits(self):
        self.marker()
        with self.assertRaisesRegex(RuntimeError, "already reserved/consumed"):
            recovery.submit_once(self.c)
        self.assertFalse(self.mutators())
        self.run_main("recovery-status")

    def test_unapproved_submission_never_reserves_or_submits(self):
        with patch("builtins.input", return_value="NO"):
            with self.assertRaisesRegex(RuntimeError, "not approved"):
                recovery.submit_once(self.c)
        self.assertFalse(recovery.recovery_path().exists())
        self.assertFalse(self.mutators())

    def test_async_single_submission_preserves_both_receipts(self):
        with patch("builtins.input", side_effect=[recovery.SUBMIT_APPROVAL, "UPLOAD-REVIEWED"]):
            recovery.submit_once(self.c)
        submits = [a for a in self.commands if a[:2] == ("builds", "submit")]
        self.assertEqual(len(submits), 1)
        self.assertIn("--async", submits[0])
        self.assertFalse(any(a[:3] == ("run", "jobs", "create") for a in self.commands))
        self.assertTrue(recovery.original_path().exists())
        self.assertTrue(recovery.recovery_path().exists())
        self.assertEqual(
            json.loads(recovery.event_path("submitted").read_text())["replacement_build_id"],
            self.replacement_id,
        )
        with self.assertRaisesRegex(RuntimeError, "already reserved/consumed"):
            recovery.submit_once(self.c)
        self.assertEqual(len([a for a in self.commands if a[:2] == ("builds", "submit")]), 1)

    def test_application_checkout_is_detached_at_reviewed_revision(self):
        with patch("builtins.input", side_effect=[recovery.SUBMIT_APPROVAL, "UPLOAD-REVIEWED"]):
            recovery.submit_once(self.c)
        self.git_mock.assert_any_call(
            "clone", "--no-checkout", "--branch", "main", "--single-branch", recovery.REPO_URL, ANY
        )
        self.git_mock.assert_any_call("checkout", "--detach", recovery.REV, cwd=ANY)

    def test_changed_application_revision_stops_before_attempt(self):
        def changed(*args, **kwargs):
            if args[:2] == ("rev-parse", "HEAD"):
                return "0" * 40
            return self.fake_git(*args, **kwargs)

        self.git_mock.side_effect = changed
        with patch("builtins.input", return_value=recovery.SUBMIT_APPROVAL):
            with self.assertRaisesRegex(RuntimeError, "reviewed clean revision"):
                recovery.submit_once(self.c)
        self.assertFalse(recovery.recovery_path().exists())
        self.assertFalse(self.mutators())

    def test_acknowledgement_lost_preserves_reservation_never_resubmits(self):
        self.submit_error = TimeoutError("Lost acknowledgement")
        with patch("builtins.input", side_effect=[recovery.SUBMIT_APPROVAL, "UPLOAD-REVIEWED"]):
            with self.assertRaises(TimeoutError):
                recovery.submit_once(self.c)
        self.assertTrue(recovery.recovery_path().exists())
        self.assertTrue(recovery.event_path("submission-uncertain").exists())
        with self.assertRaisesRegex(RuntimeError, "already reserved/consumed"):
            recovery.submit_once(self.c)
        self.assertEqual(len([a for a in self.commands if a[:2] == ("builds", "submit")]), 1)

    def test_lost_acknowledgement_reconciles_existing_server_build_without_resubmit(self):
        self.submit_error = TimeoutError("Lost acknowledgement")
        with patch("builtins.input", side_effect=[recovery.SUBMIT_APPROVAL, "UPLOAD-REVIEWED"]):
            with self.assertRaises(TimeoutError):
                recovery.submit_once(self.c)
        self.extra = self.build(self.replacement_id, "WORKING", recovery.RECOVERY_TAG)
        self.extra["createTime"] = dt.datetime.now(dt.UTC).isoformat()
        self.run_main("recovery-status")
        self.assertEqual(recovery.snapshot(self.c)["replacement_build"]["id"], self.replacement_id)
        self.assertFalse(recovery.event_path("submitted").exists())
        with self.assertRaisesRegex(RuntimeError, "already reserved/consumed"):
            recovery.submit_once(self.c)
        self.assertEqual(len([a for a in self.commands if a[:2] == ("builds", "submit")]), 1)

    def test_active_interrupted_build_is_status_only(self):
        self.marker()
        self.extra = self.build(self.replacement_id, "WORKING", recovery.RECOVERY_TAG)
        names = {p.name for p in recovery.state_root().iterdir()}
        self.run_main("recovery-status")
        self.assertEqual({p.name for p in recovery.state_root().iterdir()}, names)
        with self.assertRaisesRegex(RuntimeError, "not successful"):
            recovery.deploy_replacement(self.c)
        self.assertFalse(self.mutators())

    def test_failed_cancelled_timeout_replacements_never_deploy(self):
        self.marker()
        for status in ("FAILURE", "CANCELLED", "TIMEOUT", "INTERNAL_ERROR", "QUEUED"):
            self.extra = self.build(self.replacement_id, status, recovery.RECOVERY_TAG)
            with (
                self.subTest(status=status),
                self.assertRaisesRegex(RuntimeError, "not successful"),
            ):
                recovery.deploy_replacement(self.c)
        self.assertFalse(self.mutators())

    def test_successful_replacement_exact_digest_selected(self):
        self.success()
        self.assertEqual(recovery.recovery_image(self.c), self.digest)
        self.assertFalse(self.mutators())

    def test_successful_build_but_failed_docker_step_rejected(self):
        self.success()
        self.extra["steps"][0]["status"] = "FAILURE"
        with self.assertRaisesRegex(RuntimeError, "not successful"):
            recovery.recovery_image(self.c)

    def test_digest_mismatch_prevents_job_creation(self):
        self.success()
        self.images = [{"uri": self.uri + "@sha256:" + "b" * 64}]
        with self.assertRaisesRegex(RuntimeError, "inventory differs"):
            recovery.deploy_replacement(self.c)
        self.assertFalse(self.mutators())

    def test_registry_tag_mismatch_prevents_job_creation(self):
        self.success()
        original = self.fake_gc

        def changed(*args, **kwargs):
            if args[:4] == ("artifacts", "docker", "images", "describe"):
                return {"image_summary": {"digest": "sha256:" + "b" * 64}}
            return original(*args, **kwargs)

        with patch.object(recovery, "gc", side_effect=changed):
            with self.assertRaisesRegex(RuntimeError, "tag differs"):
                recovery.deploy_replacement(self.c)
        self.assertFalse(self.mutators())

    def test_replacement_wrong_tag_not_adopted(self):
        self.marker()
        self.extra = self.build(self.replacement_id, "SUCCESS", recovery.REV)
        with self.assertRaisesRegex(RuntimeError, "substitutions"):
            recovery.snapshot(self.c)

    def test_replacement_predating_reservation_rejected(self):
        self.success()
        self.extra["createTime"] = "2026-10-30T09:59:59Z"
        with self.assertRaisesRegex(RuntimeError, "predates"):
            recovery.snapshot(self.c)

    def test_submitted_receipt_wrong_build_id_rejected(self):
        self.success()
        recovery.write_new_receipt(
            recovery.event_path("submitted"), {"replacement_build_id": recovery.KNOWN_CANCELLED}
        )
        with self.assertRaisesRegex(RuntimeError, "build ID mismatch"):
            recovery.snapshot(self.c)

    def test_stage2_and_stage3_use_shared_recovery_digest_selector(self):
        self.success()
        self.assertEqual(stage2.approved_image(self.c), self.digest)
        self.assertEqual(stage3.approved_image(self.c), self.digest)
        self.assertFalse(self.mutators())

    def test_legacy_build_deploy_action_disabled(self):
        with patch("sys.argv", ["stage2.py", "build-deploy"]):
            with self.assertRaisesRegex(RuntimeError, "disabled"):
                stage2.main()
        self.assertFalse(self.mutators())

    def test_deployment_requires_separate_approval(self):
        self.success()
        with patch("builtins.input", return_value="NO"):
            with self.assertRaisesRegex(RuntimeError, "not approved"):
                recovery.deploy_replacement(self.c)
        self.assertFalse(self.mutators())
        self.assertFalse(recovery.event_path("verified-build").exists())

    def test_success_deploys_digest_once_without_execution_or_rebuild(self):
        self.success()
        with patch("builtins.input", return_value=recovery.DEPLOY_APPROVAL):
            recovery.deploy_replacement(self.c)
        creates = [a for a in self.commands if a[:3] == ("run", "jobs", "create")]
        self.assertEqual(len(creates), 1)
        self.assertIn("--image=" + self.uri + "@" + self.digest, creates[0])
        self.assertFalse(
            any(
                a[:2] == ("builds", "submit") or a[:3] == ("run", "jobs", "execute")
                for a in self.commands
            )
        )
        self.assertTrue(recovery.event_path("verified-build").exists())
        self.assertTrue(recovery.event_path("job-created").exists())
        recovery.deploy_replacement(self.c)
        self.assertEqual(len([a for a in self.commands if a[:3] == ("run", "jobs", "create")]), 1)

    def test_success_status_still_writes_no_receipts(self):
        self.success()
        names = {p.name for p in recovery.state_root().iterdir()}
        self.run_main("recovery-status")
        self.assertEqual({p.name for p in recovery.state_root().iterdir()}, names)
        self.assertFalse(self.mutators())


if __name__ == "__main__":
    unittest.main()
