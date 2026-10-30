"""Offline checks of the workflows' security contract and unchanged application."""

import ast
import hashlib
import json
import re
import sys
import unittest
from pathlib import Path

import bootstrap
import settings


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tools = Path(__file__).resolve().parent
        self.root = self.tools.parents[1]

    def test_workflows_have_manual_main_only_trust_and_separate_gates(self):
        for action, plan in zip(
            ("deploy", "execute"), bootstrap.identity_plan()["pools"], strict=True
        ):
            text = (self.root / f".github/workflows/gcp-{action}.yml").read_text()
            for expected in (
                "workflow_dispatch:",
                "github.ref == 'refs/heads/main'",
                f"github.repository_id == '{settings.REPO_ID}'",
                f"github.repository_owner_id == '{settings.OWNER_ID}'",
                f"environment: gcp-{action}",
                "group: gcp-synthetic-quality-control",
                "cancel-in-progress: false",
                "id-token: write",
                "contents: read",
                "persist-credentials: false",
                "access_token_lifetime: 300s",
                "create_credentials_file: false",
                "export_environment_variables: false",
                f'owner_attested=config["environment_bypass_disabled_attested"]["gcp-{action}"]',
            ):
                self.assertIn(expected, text)
            self.assertNotRegex(text, r"(?m)^\s+(?:push|pull_request|schedule|workflow_run):")
            provider = re.search(r"(?m)^\s+workload_identity_provider: (.+)$", text).group(1)
            audience = re.search(r"(?m)^\s+audience: (.+)$", text).group(1)
            self.assertEqual(provider, plan["provider"]["name"])
            self.assertEqual([audience], plan["provider"]["allowedAudiences"])
            self.assertLess(
                text.index("load_config(require_enabled=True)"),
                text.index("uses: google-github-actions/auth@"),
            )
            self.assertLess(
                text.index("verify_gate("), text.index("uses: google-github-actions/auth@")
            )
            self.assertNotIn("credentials_json", text)
            self.assertNotIn("builds submit", text)
            self.assertNotRegex(text, r"(?m)^\s*run:.*\$\{\{\s*inputs\.")

    def test_reviewable_authorization_plan_matches_generated_permissions_and_trust(self):
        recorded = json.loads((self.tools / "authorization-plan.json").read_text())
        self.assertEqual(recorded, bootstrap.identity_plan())

    def test_all_actions_are_pinned_to_immutable_commits(self):
        for name in ("gcp-deploy.yml", "gcp-execute.yml", "oidc-checks.yml"):
            text = (self.root / ".github/workflows" / name).read_text()
            for ref in re.findall(r"uses:\s*([^\s#]+)", text):
                self.assertRegex(ref, r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")

    def test_offline_ci_has_no_federation_permission_or_credentials(self):
        text = (self.root / ".github/workflows/oidc-checks.yml").read_text()
        self.assertNotIn("id-token:", text)
        self.assertNotIn("google-github-actions/auth", text)
        self.assertNotIn("secrets.", text)
        self.assertIn("unittest discover -s deploy/oidc", text)

    def test_original_application_blobs_remain_unchanged(self):
        baseline = json.loads((self.tools / "application-baseline.json").read_text())
        self.assertEqual(baseline["application_commit"], settings.APP_REV)
        for entry in baseline["files"]:
            data = (self.root / entry["path"]).read_bytes()
            actual = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
            self.assertEqual(actual, entry["git_blob_sha"], entry["path"])

    def test_tools_use_only_standard_library_and_sibling_modules(self):
        allowed = sys.stdlib_module_names | {p.stem for p in self.tools.glob("*.py")}
        for path in self.tools.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    names = [x.name.split(".")[0] for x in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module.split(".")[0]]
                else:
                    continue
                for name in names:
                    self.assertIn(name, allowed, f"Unexpected dependency in {path.name}")

    def test_runtime_and_setup_do_not_access_recovery_receipts_or_build(self):
        for name in ("bootstrap.py", "job_control.py", "settings.py"):
            text = (self.tools / name).read_text()
            self.assertNotIn(".local/state", text)
            self.assertNotIn('"builds", "submit"', text)
            self.assertNotIn('"keys", "create"', text)


if __name__ == "__main__":
    unittest.main()
