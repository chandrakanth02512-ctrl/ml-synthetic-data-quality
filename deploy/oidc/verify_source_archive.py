"""Offline archive proof against owner-supplied provenance and pinned Git blobs.

Reads local files only. Never extracts, executes, downloads, writes evidence,
changes activation gates or accesses credentials/recovery receipts.
"""

import argparse
import base64
import binascii
import gzip
import hashlib
import io
import json
import re
import sys
import tarfile
from pathlib import Path

from settings import (
    APP_REV,
    IMAGE,
    IMAGE_BASE,
    KNOWN_CANCELLED,
    PROJECT,
    PROJECT_NUMBER,
    REGION,
    REPOSITORY,
    image_uri,
    load_config,
)

TOOLS = Path(__file__).resolve().parent
MAX_ARCHIVE = 32 * 1024 * 1024
MAX_MEMBER = 4 * 1024 * 1024
MAX_TOTAL = 16 * 1024 * 1024
MAX_ENTRIES = 256
MAX_TAR = MAX_TOTAL + 2 * 1024 * 1024
ROOT_FILES = {
    ".gcloudignore",
    ".dockerignore",
    "Dockerfile",
    "cloudbuild.yaml",
    "pyproject.toml",
    "requirements-cloud.txt",
    "README.md",
}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def blob_sha(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def expected_sources():
    baseline = json.loads((TOOLS / "application-baseline.json").read_text())
    require(baseline["application_commit"] == APP_REV, "Reference application commit differs")
    expected = {
        item["path"]: item["git_blob_sha"]
        for item in baseline["files"]
        if item["path"] in ROOT_FILES
        or re.fullmatch(r"src/synthetic_quality/[^/]+\.py", item["path"])
    }
    require(ROOT_FILES <= expected.keys(), "Reference source allowlist is incomplete")
    return expected


def validate_evidence(evidence, config):
    require(
        evidence.get("project_id") == PROJECT
        and evidence.get("region") == REGION
        and evidence.get("replacement_build_id") == config["replacement_build_id"]
        and evidence.get("replacement_build_id") != KNOWN_CANCELLED
        and evidence.get("status") == "SUCCESS"
        and evidence.get("reported_image_uri") == image_uri(config),
        "Evidence build/project/image identity differs from reviewed configuration",
    )
    require(
        evidence.get("serviceAccount") == f"projects/{PROJECT}/serviceAccounts/"
        f"synthetic-quality-build@{PROJECT}.iam.gserviceaccount.com",
        "Dedicated build identity differs",
    )
    require(
        evidence.get("substitutions")
        == {
            "_IMAGE": IMAGE,
            "_REGION": REGION,
            "_REPOSITORY": REPOSITORY,
            "_TAG": APP_REV + "-recovery1",
        },
        "Build substitutions differ",
    )
    require(
        evidence.get("steps")
        == [
            {
                "name": "gcr.io/cloud-builders/docker",
                "status": "SUCCESS",
                "env": ["DOCKER_BUILDKIT=1"],
                "args": [
                    "build",
                    "--platform=linux/amd64",
                    "--tag=" + IMAGE_BASE + ":" + APP_REV + "-recovery1",
                    ".",
                ],
            }
        ],
        "Docker steps or context differ from pinned build configuration",
    )
    source = evidence.get("source", {}).get("storageSource", {})
    provenance = evidence.get("sourceProvenance", {})
    require(
        set(source) == {"bucket", "object", "generation"}
        and source == provenance.get("resolvedStorageSource")
        and source.get("bucket") == f"synthetic-quality-{PROJECT_NUMBER}-source-as1"
        and source.get("object", "").startswith("source/recovery-" + KNOWN_CANCELLED + "/")
        and re.fullmatch(r"[A-Za-z0-9_./-]+\.tgz", source.get("object", ""))
        and ".." not in source.get("object", "").split("/")
        and re.fullmatch(r"[1-9][0-9]*", source.get("generation", "")),
        "Submitted/resolved source object or immutable generation differs",
    )
    uri = f"gs://{source['bucket']}/{source['object']}#{source['generation']}"
    hashes = provenance.get("fileHashes", {})
    require(set(hashes) == {uri}, "Source hashes do not identify the exact resolved generation")
    decoded = {}
    for entry in hashes[uri].get("fileHash", []):
        kind = entry.get("type")
        require(kind in {"SHA256", "MD5"} and kind not in decoded, "Unexpected/duplicate hash")
        try:
            value = base64.b64decode(entry.get("value", ""), altchars=b"-_", validate=True)
        except (binascii.Error, ValueError):
            raise RuntimeError("Invalid source hash encoding") from None
        require(len(value) == {"SHA256": 32, "MD5": 16}[kind], "Invalid source hash length")
        decoded[kind] = value.hex()
    require("SHA256" in decoded, "Source SHA256 required; MD5 alone cannot prove archive integrity")
    return uri, decoded


def member_name(member):
    name = member.name
    require(
        "\\" not in name and "\0" not in name and not name.startswith("/"),
        "Unsafe archive member path",
    )
    while name.startswith("./"):
        name = name[2:]
    if member.isdir():
        name = name.rstrip("/")
        if name in {"", "."}:
            return ""
    require(
        all(part not in {"", ".", ".."} for part in name.split("/")), "Unsafe archive member path"
    )
    return name


def verify_archive(path, evidence, config, expected=None):
    uri, hashes = validate_evidence(evidence, config)
    expected = expected_sources() if expected is None else expected
    # Bounded snapshot makes hashing and parsing use the same bytes, without extraction.
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_ARCHIVE + 1)
    require(len(raw) <= MAX_ARCHIVE, "Archive exceeds reviewed size bound")
    require(
        hashlib.sha256(raw).hexdigest() == hashes["SHA256"],
        "Archive SHA256 differs from consumed-source provenance",
    )
    if "MD5" in hashes:
        require(
            hashlib.md5(raw, usedforsecurity=False).hexdigest() == hashes["MD5"],
            "Archive MD5 differs from supplied provenance",
        )
    seen, files, total = set(), set(), 0
    directories = {"", "src", "src/synthetic_quality"}
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
        tar_bytes = compressed.read(MAX_TAR + 1)
    require(len(tar_bytes) <= MAX_TAR, "Decompressed archive exceeds size bound")
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as archive:
        for index, member in enumerate(archive, 1):
            require(index <= MAX_ENTRIES, "Too many archive members")
            name = member_name(member)
            require(name not in seen, "Duplicate archive member")
            seen.add(name)
            if member.isdir():
                require(name in directories and member.size == 0, "Unexpected archive directory")
                continue
            require(
                member.isfile() and not member.issparse(),
                "Archive links, devices, sparse or special members are forbidden",
            )
            require(name in expected, "Unexpected source input: " + name)
            require(member.mode & 0o7111 == 0, "Unexpected executable or special source mode")
            require(0 <= member.size <= MAX_MEMBER, "Source member exceeds size bound")
            total += member.size
            require(total <= MAX_TOTAL, "Source contents exceed total size bound")
            data = archive.extractfile(member).read(MAX_MEMBER + 1)
            require(
                len(data) == member.size and blob_sha(data) == expected[name],
                "Source bytes differ from pinned Git blob: " + name,
            )
            files.add(name)
        footer = tar_bytes[archive.offset :]
        require(
            len(footer) >= 1024 and not any(footer),
            "Unexpected trailing archive data or missing end markers",
        )
    require(
        files == set(expected),
        "Missing pinned source inputs: " + ", ".join(sorted(set(expected) - files)),
    )
    return {
        "status": "ARCHIVE_CONTENTS_MATCH_PINNED_APPLICATION",
        "application_commit": APP_REV,
        "replacement_build_id": config["replacement_build_id"],
        "image_uri": image_uri(config),
        "source_uri": uri,
        "source_archive_sha256": hashes["SHA256"],
        "matched_files": len(files),
        "basis": "Owner-supplied provenance plus local archive; no live query or activation",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", help="Local copy of the exact consumed GCS object generation")
    args = parser.parse_args()
    evidence = json.loads((TOOLS / "source-evidence.json").read_text())
    result = verify_archive(args.archive, evidence, load_config())
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (
        RuntimeError,
        ValueError,
        OSError,
        KeyError,
        TypeError,
        tarfile.TarError,
        EOFError,
    ) as error:
        sys.exit("STOP: " + str(error))
