"""Offline repository checks; no credentials, Git commands or cloud clients."""

import ast
import hashlib
import json
import sys
import unittest
from pathlib import Path

import preflight


class RepositoryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tools = Path(__file__).resolve().parent
        self.root = self.tools.parents[1]

    def test_existing_application_files_match_reviewed_git_blobs(self):
        baseline = json.loads((self.tools / "application-baseline.json").read_text())
        self.assertEqual(baseline["application_commit"], preflight.REV)
        for item in baseline["files"]:
            with self.subTest(path=item["path"]):
                data = (self.root / item["path"]).read_bytes()
                actual = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
                self.assertEqual(actual, item["git_blob_sha"])

    def test_recovery_modules_have_only_standard_library_dependencies(self):
        local = {p.stem for p in self.tools.glob("*.py")}
        allowed = sys.stdlib_module_names | local
        for path in self.tools.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module.split(".")[0]]
                else:
                    continue
                for name in names:
                    self.assertIn(name, allowed, f"Unexpected dependency {name} in {path.name}")

    def test_receipts_and_private_artifacts_are_not_in_recovery_directory(self):
        for path in self.tools.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                self.assertNotIn(path.suffix, {".attempt", ".pem", ".key", ".zip", ".joblib"})
                self.assertNotEqual(path.name, "application_default_credentials.json")


if __name__ == "__main__":
    unittest.main()
