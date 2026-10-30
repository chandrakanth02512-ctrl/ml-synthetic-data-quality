"""Local synthetic archives only; supplied live archive bytes are not available."""

import base64
import contextlib
import copy
import gzip
import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import settings
import verify_source_archive as proof


class SourceArchiveTests(unittest.TestCase):
    def setUp(self):
        self.config = settings.load_config()
        self.evidence = json.loads((proof.TOOLS / "source-evidence.json").read_text())
        self.uri, _ = proof.validate_evidence(self.evidence, self.config)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.folder = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.path = self.folder / "source.tgz"
        self.stack.enter_context(patch("socket.socket", side_effect=AssertionError("No network")))
        self.stack.enter_context(patch("subprocess.run", side_effect=AssertionError("No process")))
        self.stack.enter_context(
            patch.object(tarfile.TarFile, "extract", side_effect=AssertionError("No extraction"))
        )
        self.stack.enter_context(
            patch.object(tarfile.TarFile, "extractall", side_effect=AssertionError("No extraction"))
        )
        self.contents = {"Dockerfile": b"reviewed source\n"}
        self.expected = {k: proof.blob_sha(v) for k, v in self.contents.items()}

    def archive(self, entries=None):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            if entries is None:
                entries = [(name, data, tarfile.REGTYPE) for name, data in self.contents.items()]
            for name, data, kind in entries:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.size = len(data) if kind == tarfile.REGTYPE else 0
                if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                    member.linkname = "outside"
                archive.addfile(member, io.BytesIO(data) if member.size else None)
        return stream.getvalue()

    def fixture(self, raw):
        self.path.write_bytes(raw)
        evidence = copy.deepcopy(self.evidence)
        evidence["sourceProvenance"]["fileHashes"][self.uri]["fileHash"] = [
            {"type": "SHA256", "value": base64.b64encode(hashlib.sha256(raw).digest()).decode()},
            {
                "type": "MD5",
                "value": base64.b64encode(
                    hashlib.md5(raw, usedforsecurity=False).digest()
                ).decode(),
            },
        ]
        return evidence

    def check(self, raw, expected=None):
        return proof.verify_archive(
            self.path,
            self.fixture(raw),
            self.config,
            self.expected if expected is None else expected,
        )

    def test_supplied_hashes_decode_without_implying_source_contents(self):
        uri, hashes = proof.validate_evidence(self.evidence, self.config)
        self.assertTrue(uri.endswith("#1793365313077688"))
        self.assertEqual(
            hashes["SHA256"], "84d69e14c3934d8965edd6c734417710816cb20a615d713841bc21c68612519e"
        )
        self.assertEqual(hashes["MD5"], "40a096287045897d5c441ef6b88968fe")
        self.assertFalse(self.config["enabled"])

    def test_reference_inventory_matches_pinned_upload_allowlist(self):
        expected = proof.expected_sources()
        self.assertEqual(len(expected), 19)
        self.assertFalse(any(p.startswith(("deploy/", "tests/", ".git/")) for p in expected))
        self.assertTrue(proof.ROOT_FILES <= expected.keys())

    def test_full_pinned_fixture_matches_without_extraction_or_activation(self):
        root = proof.TOOLS.parents[1]
        self.contents = {name: (root / name).read_bytes() for name in proof.expected_sources()}
        result = self.check(self.archive(), proof.expected_sources())
        self.assertEqual(result["matched_files"], 19)
        self.assertEqual(result["application_commit"], settings.APP_REV)
        self.assertIn("no live query or activation", result["basis"])
        self.assertFalse(settings.load_config()["enabled"])
        self.assertEqual(list(self.folder.iterdir()), [self.path])

    def test_archive_sha256_is_checked_before_parsing(self):
        self.path.write_bytes(b"not the consumed archive")
        with patch.object(tarfile, "open", side_effect=AssertionError("Do not parse")):
            with self.assertRaisesRegex(RuntimeError, "Archive SHA256"):
                proof.verify_archive(self.path, self.evidence, self.config, self.expected)

    def test_changed_source_with_valid_fixture_archive_hash_is_rejected(self):
        raw = self.archive([("Dockerfile", b"changed source", tarfile.REGTYPE)])
        with self.assertRaisesRegex(RuntimeError, "pinned Git blob"):
            self.check(raw)

    def test_extra_and_missing_inputs_are_rejected(self):
        for entries, message in [
            ([("private.env", b"extra", tarfile.REGTYPE)], "Unexpected source"),
            ([], "Missing pinned"),
        ]:
            with self.subTest(entries=entries), self.assertRaisesRegex(RuntimeError, message):
                self.check(self.archive(entries))

    def test_links_and_special_members_are_rejected(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.FIFOTYPE):
            with self.subTest(kind=kind), self.assertRaisesRegex(RuntimeError, "special members"):
                self.check(self.archive([("Dockerfile", b"", kind)]))

    def test_unsafe_member_paths_are_rejected(self):
        for name in ("../Dockerfile", "/Dockerfile", "src\\Dockerfile", "src/../Dockerfile"):
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, "Unsafe archive"):
                self.check(self.archive([(name, b"unsafe", tarfile.REGTYPE)]))

    def test_duplicate_normalized_paths_are_rejected(self):
        entries = [
            (name, self.contents["Dockerfile"], tarfile.REGTYPE)
            for name in ("Dockerfile", "./Dockerfile")
        ]
        with self.assertRaisesRegex(RuntimeError, "Duplicate"):
            self.check(self.archive(entries))

    def test_submitted_and_resolved_generation_must_agree(self):
        evidence = copy.deepcopy(self.evidence)
        evidence["source"]["storageSource"]["generation"] = "1"
        with self.assertRaisesRegex(RuntimeError, "immutable generation"):
            proof.validate_evidence(evidence, self.config)

    def test_build_identity_step_substitutions_and_hash_key_drift_are_rejected(self):
        variants = []
        for field, value in (
            ("replacement_build_id", settings.KNOWN_CANCELLED),
            ("reported_image_uri", "image:latest"),
            ("serviceAccount", "unexpected"),
        ):
            evidence = copy.deepcopy(self.evidence)
            evidence[field] = value
            variants.append(evidence)
        evidence = copy.deepcopy(self.evidence)
        evidence["steps"][0]["entrypoint"] = "sh"
        variants.append(evidence)
        evidence = copy.deepcopy(self.evidence)
        evidence["substitutions"]["_TAG"] = "another-revision"
        variants.append(evidence)
        evidence = copy.deepcopy(self.evidence)
        evidence["sourceProvenance"]["fileHashes"]["wrong-generation"] = evidence[
            "sourceProvenance"
        ]["fileHashes"].pop(self.uri)
        variants.append(evidence)
        for evidence in variants:
            with self.subTest(evidence=evidence), self.assertRaises(RuntimeError):
                proof.validate_evidence(evidence, self.config)

    def test_source_sha256_is_required_and_hash_encoding_is_validated(self):
        for entries in (
            [{"type": "MD5", "value": "QKCWKHBFiX1cRB72uIlo_g=="}],
            [{"type": "SHA256", "value": "invalid!"}],
            [{"type": "SHA256", "value": "YQ=="}],
            [{"type": "SHA256", "value": "hNaeFMOTTYll7dbHNEF3EIFssgphXXE4QbwhxoYSUZ4="}] * 2,
        ):
            evidence = copy.deepcopy(self.evidence)
            evidence["sourceProvenance"]["fileHashes"][self.uri]["fileHash"] = entries
            with self.subTest(entries=entries), self.assertRaises(RuntimeError):
                proof.validate_evidence(evidence, self.config)

    def test_compressed_and_decompressed_size_bounds(self):
        for limit, message in (
            ("MAX_ARCHIVE", "Archive exceeds"),
            ("MAX_TAR", "Decompressed archive"),
            ("MAX_MEMBER", "member exceeds"),
            ("MAX_TOTAL", "total size"),
            ("MAX_ENTRIES", "Too many"),
        ):
            with self.subTest(limit=limit), patch.object(proof, limit, 0):
                with self.assertRaisesRegex(RuntimeError, message):
                    self.check(self.archive())

    def test_unexpected_directories_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "Unexpected archive directory"):
            self.check(self.archive([("unrelated/", b"", tarfile.DIRTYPE)]))

    def test_concatenated_hidden_archive_is_rejected(self):
        raw = gzip.compress(gzip.decompress(self.archive()) + b"hidden trailing content")
        with self.assertRaisesRegex(RuntimeError, "trailing archive data"):
            self.check(raw)

    def test_missing_local_archive_does_not_create_one(self):
        with self.assertRaises(FileNotFoundError):
            proof.verify_archive(self.path, self.evidence, self.config)
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_executable_and_special_file_modes_are_rejected(self):
        for mode in (0o755, 0o4644, 0o2644, 0o1644):
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode="w:gz") as archive:
                member = tarfile.TarInfo("Dockerfile")
                member.mode = mode
                member.size = len(self.contents["Dockerfile"])
                archive.addfile(member, io.BytesIO(self.contents["Dockerfile"]))
            with self.subTest(mode=mode), self.assertRaisesRegex(RuntimeError, "source mode"):
                self.check(stream.getvalue())


if __name__ == "__main__":
    unittest.main()
