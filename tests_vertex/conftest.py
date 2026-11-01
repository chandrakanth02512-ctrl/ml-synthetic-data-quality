"""Contract fixtures share PR #4's fake SDK boundaries; no live credentials."""

import json

import pytest

from synthetic_quality.data_generator import generate_transactions
from synthetic_quality.schema import UNITS_PER_USD
from synthetic_quality_lakehouse.config import RunConfig, canonical
from synthetic_quality_lakehouse.source import parse_source
from synthetic_quality_vertex.config import ObjectRef, TrainingConfig, fingerprint
from synthetic_quality_vertex.custom_job import VertexObjects
from tests_lakehouse.conftest import Client


@pytest.fixture(autouse=True)
def no_cloud(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No live credentials, cloud clients or HTTP in offline tests")

    monkeypatch.setattr("google.cloud.storage.Client", forbidden)
    monkeypatch.setattr("google.cloud.bigquery.Client", forbidden)
    monkeypatch.setattr("google.auth.default", forbidden)
    monkeypatch.setattr("requests.sessions.Session.request", forbidden)


@pytest.fixture
def store():
    return VertexObjects(Client())


def reference(store, uri, data):
    return ObjectRef(**store.create(uri, data))


def split(store, name, frame, extra=None):
    records = frame.to_dict("records")
    for row in records:
        row["timestamp"] = row["timestamp"].isoformat()
    data = b"".join(canonical(r) + b"\n" for r in records)
    src = reference(store, "gs://offline-data/lakehouse/inputs/" + name + ".ndjson", data)
    cfg = RunConfig(src.uri, src.generation, src.sha256, "gs://offline-data/lakehouse/phase2", name)
    req = cfg.request("4.0.1")
    fp = fingerprint(req)
    clean, ledger = [], []
    for row, parsed in zip(records, parse_source(data, src.sha256), strict=True):
        meta = {
            "run_id": name,
            "source_fingerprint": fp,
            **{
                k: parsed[k] for k in ("source_row_id", "source_row_number", "source_record_sha256")
            },
        }
        amount = row["transaction_amount"] / UNITS_PER_USD[row["currency"]]
        clean.append(
            {
                **meta,
                **row,
                "transaction_amount_usd": amount,
                "transaction_date": row["timestamp"][:10],
                **(extra or {}),
            }
        )
        ledger.append({**meta, "disposition": "clean", "amount_usd": amount})
    from synthetic_quality_lakehouse.spark import CHECKS

    quality = [
        {
            "run_id": name,
            "source_fingerprint": fp,
            "check_name": c,
            "evaluated_rows": len(records),
            "failed_rows": 0,
            "passed": True,
        }
        for c in CHECKS
    ]
    artifacts = {}
    for kind, values in (
        ("clean", clean),
        ("quarantine", []),
        ("quality", quality),
        ("ledger", ledger),
    ):
        artifacts[kind] = {
            **store.create(
                cfg.root + "/" + kind + ".ndjson", b"".join(canonical(r) + b"\n" for r in values)
            ),
            "row_count": len(values),
        }
    manifest = {
        "status": "COMPLETE",
        "request": req,
        "fingerprint": fp,
        "source_rows": len(records),
        "artifacts": artifacts,
    }
    return reference(store, cfg.root + "/completion.json", canonical(manifest))


@pytest.fixture
def data_pair():
    return (
        generate_transactions(256, seed=42, dataset_name="training"),
        generate_transactions(
            100, seed=123, anomaly_fraction=0.05, dataset_name="evaluation", start_date="2025-02-01"
        ),
    )


@pytest.fixture
def config(store, data_pair):
    training, evaluation = data_pair
    train_ref = split(store, "training-001", training.transactions)
    eval_ref = split(store, "evaluation-001", evaluation.transactions)
    rows = evaluation.truth.to_dict("records")
    for row in rows:
        row["is_anomaly"] = int(row["is_anomaly"])
    truth = reference(
        store,
        "gs://offline-data/lakehouse/vertex/truth/evaluation.ndjson",
        b"".join(canonical(r) + b"\n" for r in rows),
    )
    return TrainingConfig(
        "vertex-001", train_ref, eval_ref, truth, "gs://offline-data/lakehouse/vertex"
    )


@pytest.fixture
def trained(config, store):
    from synthetic_quality_vertex.train import run

    return run(config, store)


def artifact(store, manifest, name):
    return json.loads(store.verify(manifest["artifacts"][name]))
