"""Offline safety checks. All cloud command/API entry points are mocked."""

import contextlib
import copy
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import stage1


class BucketBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.c = SimpleNamespace(
            number="123456789012",
            data="synthetic-quality-123456789012-data-as1",
            source="synthetic-quality-123456789012-source-as1",
            repository="synthetic-quality-phase1",
            job="synthetic-quality-phase1-smoke",
            role="projects/example/roles/syntheticQualityObjects",
            runtime="runtime@example.iam.gserviceaccount.com",
            build="build@example.iam.gserviceaccount.com",
            check_policies=lambda: None,
        )
        self.gc_patch = patch.object(stage1, "gc", side_effect=AssertionError("Unexpected command"))
        self.api_patch = patch.object(
            stage1, "api", side_effect=AssertionError("Unexpected API call")
        )
        self.gc = self.gc_patch.start()
        self.api = self.api_patch.start()
        self.addCleanup(self.gc_patch.stop)
        self.addCleanup(self.api_patch.stop)
        self.block_network = patch(
            "preflight.urllib.request.urlopen", side_effect=AssertionError("Network prohibited")
        )
        self.block_network.start()
        self.addCleanup(self.block_network.stop)
        self.spec = stage1.resources(self.c)[0]
        self.raw = {
            "name": self.c.data,
            "projectNumber": self.c.number,
            "location": "ASIA-SOUTH1",
            "storageClass": "STANDARD",
            "metageneration": "1",
            "labels": {"note": "preserve"},
            "iamConfiguration": {
                "uniformBucketLevelAccess": {"enabled": True},
                "publicAccessPrevention": "enforced",
            },
            "softDeletePolicy": {"retentionDurationSeconds": "604800"},
        }
        self.labelled = copy.deepcopy(self.raw)
        self.labelled["labels"].update(stage1.LABEL)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def test_bucket_create_commands_have_no_labels_flag(self):
        for _, _, command, kind in stage1.resources(self.c):
            if kind == "bucket":
                self.assertFalse(any(x.startswith("--labels") for x in command))

    def test_new_bucket_label_is_conditional_and_preserves_labels(self):
        self.api.side_effect = [None, self.raw, self.labelled, self.labelled]
        self.gc.side_effect = None
        stage1.ensure_resource(self.c, self.spec)
        self.gc.assert_called_once_with(*self.spec[2])
        self.assertEqual(
            [x.args[0] for x in self.api.call_args_list], ["GET", "GET", "PATCH", "GET"]
        )
        self.assertEqual(
            self.api.call_args_list[2].args,
            (
                "PATCH",
                self.spec[1] + "?ifMetagenerationMatch=1",
                {"labels": {"note": "preserve", **stage1.LABEL}},
            ),
        )

    def test_existing_matching_bucket_is_never_mutated(self):
        self.api.side_effect = [self.labelled]
        stage1.ensure_resource(self.c, self.spec)
        self.gc.assert_not_called()
        self.assertEqual(self.api.call_count, 1)

    def test_existing_unlabelled_bucket_stops_without_mutation(self):
        self.api.side_effect = [self.raw]
        with self.assertRaisesRegex(RuntimeError, "unmarked"):
            stage1.ensure_resource(self.c, self.spec)
        self.gc.assert_not_called()
        self.assertEqual(self.api.call_count, 1)

    def test_create_failure_never_labels(self):
        self.api.side_effect = [None]
        self.gc.side_effect = RuntimeError(
            "Create rejected: unsupported flag or 409 or lost acknowledgement"
        )
        with self.assertRaises(RuntimeError):
            stage1.ensure_resource(self.c, self.spec)
        self.assertEqual(self.api.call_count, 1)

    def test_denied_read_is_not_missing(self):
        self.api.side_effect = RuntimeError("PERMISSION/ACCESS DENIED")
        with self.assertRaisesRegex(RuntimeError, "DENIED"):
            stage1.ensure_resource(self.c, self.spec)
        self.gc.assert_not_called()

    def test_mismatched_new_bucket_is_not_labelled(self):
        bad = copy.deepcopy(self.raw)
        bad["location"] = "US"
        self.api.side_effect = [None, bad]
        self.gc.side_effect = None
        with self.assertRaisesRegex(RuntimeError, "location"):
            stage1.ensure_resource(self.c, self.spec)
        self.assertEqual(self.api.call_count, 2)

    def test_missing_metageneration_stops_before_label(self):
        bad = copy.deepcopy(self.raw)
        del bad["metageneration"]
        self.api.side_effect = [None, bad]
        self.gc.side_effect = None
        with self.assertRaisesRegex(RuntimeError, "metageneration"):
            stage1.ensure_resource(self.c, self.spec)
        self.assertEqual(self.api.call_count, 2)

    def test_patch_conflict_stops_without_retry(self):
        self.api.side_effect = [None, self.raw, RuntimeError("HTTP 412 metadata changed")]
        self.gc.side_effect = None
        with self.assertRaisesRegex(RuntimeError, "412"):
            stage1.ensure_resource(self.c, self.spec)
        self.assertEqual(self.api.call_count, 3)

    def test_protected_bucket_cannot_be_planned_or_iam_target(self):
        self.c.data = "gcp-idq-data-984592102391"
        for operation in (stage1.resources, stage1.binding_specs):
            with self.assertRaisesRegex(RuntimeError, "naming/reuse review"):
                operation(self.c)
        self.gc.assert_not_called()
        self.api.assert_not_called()

    def test_protected_bucket_cannot_be_ensure_target(self):
        name = "gcp-idq-data-984592102391"
        bad = (
            "bucket " + name,
            "https://storage.googleapis.com/storage/v1/b/" + name,
            ["storage", "buckets", "create", "gs://" + name],
            "bucket",
        )
        with self.assertRaisesRegex(RuntimeError, "outside"):
            stage1.ensure_resource(self.c, bad)
        self.gc.assert_not_called()
        self.api.assert_not_called()

    def test_check_mode_calls_only_read_commands_and_gets(self):
        self.api.side_effect = None
        self.api.return_value = None
        self.gc.side_effect = None
        self.gc.return_value = {"bindings": []}
        with (
            patch.object(stage1, "Context", return_value=self.c),
            patch("sys.argv", ["stage1.py", "check"]),
            patch("builtins.input", side_effect=AssertionError("No apply gate in check")),
        ):
            stage1.main()
        self.assertTrue(all(x.args[0] == "GET" for x in self.api.call_args_list))
        self.assertTrue(
            all(x.args[:2] == ("projects", "get-iam-policy") for x in self.gc.call_args_list)
        )


if __name__ == "__main__":
    unittest.main()
