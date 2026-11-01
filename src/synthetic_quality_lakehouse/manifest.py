"""Completion boundary validation, independent of Spark and cloud clients."""

import hashlib
import re

from synthetic_quality_lakehouse.config import RunConfig, canonical


def validate_manifest(manifest):
    request = manifest["request"]
    config = RunConfig(**request["config"])
    if (
        manifest.get("status") != "COMPLETE"
        or request.get("format_version") != 1
        or manifest["fingerprint"] != hashlib.sha256(canonical(request)).hexdigest()
        or set(manifest["artifacts"]) != {"clean", "quarantine", "quality", "ledger"}
    ):
        raise ValueError("Invalid completed run identity/inventory")
    counts = {}
    for kind, record in manifest["artifacts"].items():
        if (
            record["uri"] != config.root + "/" + kind + ".ndjson"
            or type(record["generation"]) is not int
            or record["generation"] < 1
            or type(record["row_count"]) is not int
            or record["row_count"] < 0
            or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])
        ):
            raise ValueError("Invalid completed artifact")
        counts[kind] = record["row_count"]
    if (
        type(manifest["source_rows"]) is not int
        or not 0 < manifest["source_rows"] <= config.max_rows
        or counts["ledger"] != manifest["source_rows"]
        or counts["clean"] + counts["quarantine"] != manifest["source_rows"]
        or counts["quality"] != 12
    ):
        raise ValueError("Source/output counts do not reconcile")
    return config
