"""Offline approval-policy guard tests; no GitHub tokens or requests are used."""

import copy
import io
import json
import unittest
import urllib.error
from unittest.mock import patch

import verify_gate as gate


class ApprovalGateTests(unittest.TestCase):
    def setUp(self):
        self.environment = {
            "name": "gcp-deploy",
            "protection_rules": [
                {
                    "type": "required_reviewers",
                    "reviewers": [{"type": "User", "reviewer": {"id": 123}}],
                    "prevent_self_review": True,
                }
            ],
            "can_admins_bypass": False,
            "deployment_branch_policy": {
                "protected_branches": False,
                "custom_branch_policies": True,
            },
        }
        self.rules = [{"id": 1, "name": "main", "type": "branch"}]

    def test_reviewed_environment_accepted(self):
        self.assertFalse(gate.validate_environment(self.environment, self.rules, "gcp-deploy"))

    def test_environment_identity_mismatch_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "identity"):
            gate.validate_environment(self.environment, self.rules, "gcp-execute")

    def test_missing_and_empty_required_reviewers_rejected(self):
        for protection_rules in ([], [{"type": "required_reviewers", "reviewers": []}]):
            bad = copy.deepcopy(self.environment)
            bad["protection_rules"] = protection_rules
            with self.assertRaisesRegex(RuntimeError, "required-reviewers"):
                gate.validate_environment(bad, self.rules, "gcp-deploy")

    def test_self_review_must_be_prevented(self):
        bad = copy.deepcopy(self.environment)
        bad["protection_rules"][0]["prevent_self_review"] = False
        with self.assertRaisesRegex(RuntimeError, "self-review"):
            gate.validate_environment(bad, self.rules, "gcp-deploy")

    def test_admin_bypass_if_reported_is_rejected(self):
        bad = copy.deepcopy(self.environment)
        bad["can_admins_bypass"] = True
        with self.assertRaisesRegex(RuntimeError, "bypass"):
            gate.validate_environment(bad, self.rules, "gcp-deploy")

    def test_unreported_bypass_blocks_without_explicit_owner_attestation(self):
        bad = copy.deepcopy(self.environment)
        del bad["can_admins_bypass"]
        for attested in (False, None, "true", 1):
            with self.assertRaisesRegex(RuntimeError, "owner attestation"):
                gate.validate_environment(bad, self.rules, "gcp-deploy", owner_attested=attested)

    def test_attested_unreported_bypass_is_identified_as_unverified(self):
        bad = copy.deepcopy(self.environment)
        del bad["can_admins_bypass"]
        self.assertTrue(
            gate.validate_environment(bad, self.rules, "gcp-deploy", owner_attested=True)
        )

    def test_reported_admin_bypass_cannot_be_overridden_by_attestation(self):
        bad = copy.deepcopy(self.environment)
        bad["can_admins_bypass"] = True
        with self.assertRaisesRegex(RuntimeError, "bypass must be disabled"):
            gate.validate_environment(bad, self.rules, "gcp-deploy", owner_attested=True)

    def test_attestation_message_does_not_claim_api_verification(self):
        bad = copy.deepcopy(self.environment)
        del bad["can_admins_bypass"]

        def getter(url, token):
            if "deployment-branch-policies" in url:
                return {"total_count": 1, "branch_policies": self.rules}
            return bad

        output = io.StringIO()
        with patch("sys.stdout", output):
            gate.verify_gate("deploy", {"GH_TOKEN": "fake"}, getter, owner_attested=True)
        self.assertIn("owner-attested", output.getvalue())
        self.assertIn("not independently API-verified", output.getvalue())

    def test_protected_branches_only_is_not_exact_main(self):
        bad = copy.deepcopy(self.environment)
        bad["deployment_branch_policy"] = {
            "protected_branches": True,
            "custom_branch_policies": False,
        }
        with self.assertRaisesRegex(RuntimeError, "selected-branch"):
            gate.validate_environment(bad, self.rules, "gcp-deploy")

    def test_missing_extra_wildcard_tag_or_untyped_rules_rejected(self):
        for rules in (
            [],
            self.rules + [{"name": "other", "type": "branch"}],
            [{"name": "*", "type": "branch"}],
            [{"name": "main", "type": "tag"}],
            [{"name": "main"}],
        ):
            with self.subTest(rules=rules):
                with self.assertRaisesRegex(RuntimeError, "explicit main"):
                    gate.validate_environment(self.environment, rules, "gcp-deploy")

    def test_complete_paginated_inventory_is_not_truncated(self):
        calls = []

        def getter(url, token):
            calls.append(url)
            if "page=1" in url:
                return {"total_count": 2, "branch_policies": [self.rules[0]]}
            return {"total_count": 2, "branch_policies": [{"name": "unsafe", "type": "branch"}]}

        result = gate.branch_rules("https://example.invalid/policies", "fake", getter)
        self.assertEqual(len(result), 2)
        self.assertEqual(len(calls), 2)
        with self.assertRaisesRegex(RuntimeError, "explicit main"):
            gate.validate_environment(self.environment, result, "gcp-deploy")

    def test_empty_page_before_total_is_failure(self):
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            gate.branch_rules(
                "https://example.invalid/policies",
                "fake",
                lambda *_: {"total_count": 1, "branch_policies": []},
            )

    def test_metadata_total_drift_or_missing_total_is_failure(self):
        for response in ({"branch_policies": []}, {"total_count": "0", "branch_policies": []}):
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                gate.branch_rules(
                    "https://example.invalid/policies", "fake", lambda *_, value=response: value
                )

    def test_gate_uses_separate_environment_and_read_getters(self):
        for action in ("deploy", "execute"):
            requests = []
            environment = copy.deepcopy(self.environment)
            environment["name"] = "gcp-" + action

            def getter(url, token, requests=requests, environment=environment):
                requests.append(url)
                if "deployment-branch-policies" in url:
                    return {"total_count": 1, "branch_policies": self.rules}
                return environment

            with patch("sys.stdout", io.StringIO()):
                gate.verify_gate(action, {"GH_TOKEN": "fake"}, getter)
            self.assertEqual(len(requests), 2)
            self.assertTrue(all("/environments/gcp-" + action in url for url in requests))

    def test_unknown_action_fails_before_get(self):
        with self.assertRaisesRegex(RuntimeError, "Unknown"):
            gate.verify_gate("build", {"GH_TOKEN": "fake"}, lambda *_: self.fail("No API call"))

    def test_request_is_get_and_does_not_print_token(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return json.dumps({"name": "gcp-deploy"}).encode()

        with patch("verify_gate.urllib.request.build_opener") as factory:
            factory.return_value.open.return_value = Response()
            result = gate.get_json(gate.API_BASE + "/environments/gcp-deploy", "fake-token")
        request = factory.return_value.open.call_args.args[0]
        self.assertEqual(request.method, "GET")
        self.assertEqual(result["name"], "gcp-deploy")

    def test_http_failures_block_and_do_not_expose_response_or_token(self):
        for code in (401, 403, 404, 429, 500):
            error = urllib.error.HTTPError(
                gate.API_BASE, code, "failure", {}, io.BytesIO(b"sensitive response fixture")
            )
            with patch("verify_gate.urllib.request.build_opener") as factory:
                factory.return_value.open.side_effect = error
                with self.assertRaisesRegex(RuntimeError, f"HTTP {code}") as captured:
                    gate.get_json(gate.API_BASE + "/environments/gcp-deploy", "fake-token")
            self.assertNotIn("fake-token", str(captured.exception))
            self.assertNotIn("sensitive response", str(captured.exception))

    def test_redirect_cannot_forward_authentication(self):
        with self.assertRaisesRegex(RuntimeError, "redirect"):
            gate.NoRedirect().redirect_request(
                None, None, 302, "Found", {}, "https://elsewhere.invalid"
            )


if __name__ == "__main__":
    unittest.main()
