"""Verify Dataproc completion, clean-row lineage and train/evaluation separation."""

import json

import pandas as pd

from synthetic_quality.quality_checks import validate_transactions
from synthetic_quality.schema import REQUIRED_COLUMNS
from synthetic_quality_lakehouse.manifest import validate_manifest
from synthetic_quality_lakehouse.schemas import schema_json
from synthetic_quality_vertex.config import decode_rows, read_ref


def dataset(store, ref, max_rows):
    manifest = json.loads(read_ref(store, ref))
    source_config = validate_manifest(manifest)
    if ref.uri != source_config.root + "/completion.json":
        raise ValueError("Expected the exact Dataproc completion object")
    record = manifest["artifacts"]["clean"]
    rows = decode_rows(store.verify(record))
    if not 1 <= len(rows) <= max_rows or len(rows) != record["row_count"]:
        raise ValueError("Clean split empty or count/size bound differs")
    expected = {f["name"] for f in schema_json("clean")}
    if any(set(r) != expected for r in rows):
        raise ValueError("Clean data schema differs; labels and extra features are forbidden")
    frame = pd.DataFrame(rows)
    if (
        not frame["run_id"].eq(source_config.run_id).all()
        or not frame["source_fingerprint"].eq(manifest["fingerprint"]).all()
        or frame["source_row_id"].isna().any()
        or frame["source_row_id"].duplicated().any()
    ):
        raise ValueError("Clean data lineage differs from Dataproc completion")
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="raise")
    validate_transactions(frame[list(REQUIRED_COLUMNS)]).raise_for_errors("Vertex input")
    return frame.sort_values("transaction_id").reset_index(drop=True), manifest


def splits(store, config):
    train, training = dataset(store, config.training, config.max_rows)
    evaluation, evaluated = dataset(store, config.evaluation, config.max_rows)
    if (
        len(train) < 2
        or training["fingerprint"] == evaluated["fingerprint"]
        or training["request"]["config"]["source_uri"]
        == evaluated["request"]["config"]["source_uri"]
        or set(train.transaction_id) & set(evaluation.transaction_id)
        or set(train.source_record_sha256) & set(evaluation.source_record_sha256)
        or train.timestamp.max() >= evaluation.timestamp.min()
    ):
        raise ValueError("Train/evaluation overlap or non-chronological split")
    return train, evaluation, training, evaluated


def truth_labels(store, config, evaluation):
    rows = decode_rows(read_ref(store, config.truth))
    required = {"transaction_id", "is_anomaly"}
    if not rows or any(
        not isinstance(r, dict) or set(r) not in (required, required | {"injection_type"})
        for r in rows
    ):
        raise ValueError("Invalid evaluation-only truth schema")
    if any(
        not isinstance(r["transaction_id"], str)
        or type(r["is_anomaly"]) is not int
        or r["is_anomaly"] not in (0, 1)
        for r in rows
    ):
        raise ValueError("Truth requires string IDs and binary integer labels")
    frame = pd.DataFrame(rows)
    if frame.transaction_id.duplicated().any() or set(frame.transaction_id) != set(
        evaluation.transaction_id
    ):
        raise ValueError("Truth IDs must match the clean evaluation split exactly")
    return frame.set_index("transaction_id").loc[evaluation.transaction_id, "is_anomaly"].to_numpy()
