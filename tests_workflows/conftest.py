"""Offline cloud boundaries plus actual preexisting data/model contracts."""

import copy
from dataclasses import asdict
from pathlib import Path

import pytest
import yaml

from synthetic_quality_lakehouse.config import canonical
from synthetic_quality_vertex.config import fingerprint
from synthetic_quality_vertex.custom_job import VertexObjects
from synthetic_quality_workflows.plan import BUCKET, PROJECT, REGION, ROOT, compile_plan, processing
from tests_lakehouse.conftest import Client
from tests_vertex.conftest import data_pair, no_cloud  # noqa: F401 -- activate credential/HTTP guard.


@pytest.fixture
def store():
    return VertexObjects(Client())


@pytest.fixture
def plan(store):
    ref = store.create(f"gs://{BUCKET}/lakehouse/inputs/train.ndjson", b"training\n")
    evaluation = store.create(f"gs://{BUCKET}/lakehouse/inputs/eval.ndjson", b"evaluation\n")
    truth = store.create(f"gs://{BUCKET}/lakehouse/inputs/truth.ndjson", b"truth\n")
    bootstrap = Path("src/synthetic_quality_workflows/bootstrap.py").read_bytes()
    return {
        "format_version": 1,
        "enabled": True,
        "run_id": "workflow-001",
        "training": ref,
        "evaluation": evaluation,
        "truth": truth,
        "package": store.create(ROOT + "/code/offline.zip", b"offline-code"),
        "bootstrap": store.create(ROOT + "/code/bootstrap.py", bootstrap),
        "runtime_image": f"{REGION}-docker.pkg.dev/{PROJECT}/synthetic-quality-phase1/"
        "synthetic-quality-workflows@sha256:" + "a" * 64,
        "subnetwork": f"projects/{PROJECT}/regions/{REGION}/subnetworks/reviewed-pilot",
        "dataset": "pilot",
        "training_rows": 256,
        "evaluation_rows": 100,
        "max_quarantined_rows": 1,
        "poll_seconds": 5,
        "poll_limit": 3,
        "revision": "b" * 40,
    }


@pytest.fixture
def bundle(plan, store):
    return {"plan": plan, "reference": store.create(ROOT + "/plans/offline.json", canonical(plan))}


@pytest.fixture
def definition(bundle):
    result = yaml.safe_load(Path("workflows/pipeline.yaml").read_text())
    result["main"]["steps"][0]["initialize"]["assign"][0]["p"] = compile_plan(**bundle)
    return result


def manifests_for_plan(plan, store, generated_pair):
    from synthetic_quality_workflows.plan import root
    from tests_vertex.conftest import reference, split

    refs = {}
    for name, generated in zip(("training", "evaluation"), generated_pair, strict=True):
        frame = generated.transactions
        rows = frame.to_dict("records")
        for row in rows:
            row["timestamp"] = row["timestamp"].isoformat()
        source = reference(
            store,
            ROOT + "/inputs/real-" + name + ".ndjson",
            b"".join(canonical(r) + b"\n" for r in rows),
        )
        # Construct exactly the Dataproc contract using the existing reusable fixture.
        temp_ref = split(store, "temporary-" + name, frame)
        import json

        manifest = json.loads(store.read(temp_ref.uri)[0])
        config = processing({**plan, name: asdict(source)}, name)
        request = config.request("4.0.1")
        manifest["request"], manifest["fingerprint"] = request, fingerprint(request)
        for kind, item in manifest["artifacts"].items():
            payload = store.read(item["uri"])[0]
            out = []
            for line in payload.splitlines():
                row = json.loads(line)
                row.update(run_id=config.run_id, source_fingerprint=manifest["fingerprint"])
                out.append(canonical(row) + b"\n")
            manifest["artifacts"][kind] = {
                **store.create(config.root + "/" + kind + ".ndjson", b"".join(out)),
                "row_count": item["row_count"],
            }
        refs[name] = store.create(config.root + "/completion.json", canonical(manifest))
        plan[name] = asdict(source)
    truth = generated_pair[1].truth.to_dict("records")
    for row in truth:
        row["is_anomaly"] = int(row["is_anomaly"])
    plan["truth"] = store.create(
        root(plan) + "/truth.ndjson", b"".join(canonical(r) + b"\n" for r in truth)
    )
    return copy.deepcopy(plan), refs
