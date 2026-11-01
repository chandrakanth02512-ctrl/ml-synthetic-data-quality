"""Reuse actual processing, training and fixed-ID publication inside managed workers."""

import argparse
import json
import time
from dataclasses import asdict

from synthetic_quality_lakehouse.config import canonical
from synthetic_quality_lakehouse.manifest import validate_manifest
from synthetic_quality_workflows.plan import (
    PROJECT,
    REGION,
    ObjectRef,
    fingerprint,
    processing,
    root,
    validate,
)


def pin(store, uri):
    data, generation = store.read(uri)
    import hashlib

    return ObjectRef(uri, generation, hashlib.sha256(data).hexdigest())


def quality(plan, split, manifest, store):
    config = validate_manifest(manifest)
    if config != processing(plan, split):
        raise ValueError("Dataproc completion differs from planned source/limits")
    for record in manifest["artifacts"].values():
        store.verify(record)
    if (
        manifest["artifacts"]["clean"]["row_count"] != plan[split + "_rows"]
        or manifest["artifacts"]["quarantine"]["row_count"] > plan["max_quarantined_rows"]
    ):
        raise ValueError("Data quality/count gate failed")
    return config


def run_spark(plan, split, store, spark_factory=None):
    validate(plan)
    if not plan["enabled"] or split not in {"training", "evaluation"}:
        raise ValueError("Disabled pilot or unexpected processing phase")
    from synthetic_quality_lakehouse.driver import run

    config = processing(plan, split)
    result = run(config, store, **({"spark_factory": spark_factory} if spark_factory else {}))
    quality(plan, split, result, store)
    return result


class BoundedJob:
    def __init__(self, job, deadline, clock):
        self.job, self.deadline, self.clock = job, deadline, clock

    def __getattr__(self, name):
        return getattr(self.job, name)

    def result(self, retry=None):
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise TimeoutError("BigQuery publication deadline exceeded; reconcile fixed job IDs")
        kwargs = {"retry": None, "timeout": remaining}
        if self.job.to_api_repr().get("configuration", {}).get("query"):
            kwargs["job_retry"] = None
        return self.job.result(**kwargs)


class BoundedBQ:
    """Bound existing loader polling and API calls without changing its job identities."""

    def __init__(self, client, seconds=240, clock=time.monotonic):
        self.client, self.clock = client, clock
        self.deadline = clock() + seconds

    def __getattr__(self, name):
        if name not in {
            "get_dataset",
            "get_table",
            "create_table",
            "get_job",
            "load_table_from_uri",
            "query",
        }:
            raise AttributeError(name)

        def call(*args, **kwargs):
            remaining = self.deadline - self.clock()
            if remaining <= 0:
                raise TimeoutError("BigQuery deadline exceeded; preserve jobs and receipts")
            result = getattr(self.client, name)(*args, **kwargs, timeout=min(30, remaining))
            return (
                BoundedJob(result, self.deadline, self.clock)
                if name
                in {
                    "get_job",
                    "load_table_from_uri",
                    "query",
                }
                else result
            )

        return call


def run_vertex(plan, store, client):
    validate(plan)
    if not plan["enabled"]:
        raise ValueError("Pilot disabled")
    from synthetic_quality_vertex.config import ObjectRef as TrainingRef
    from synthetic_quality_vertex.config import TrainingConfig, read_ref
    from synthetic_quality_vertex.train import validate_completion

    plan_hash = fingerprint(plan)
    store.create(root(plan) + "/worker-request.json", canonical({"plan_hash": plan_hash}))
    refs, manifests = {}, {}
    for split in ("training", "evaluation"):
        refs[split] = pin(store, processing(plan, split).root + "/completion.json")
        manifests[split] = json.loads(read_ref(store, refs[split]))
        quality(plan, split, manifests[split], store)
    from synthetic_quality_vertex.train import run as train

    config = TrainingConfig(
        plan["run_id"],
        TrainingRef(**asdict(refs["training"])),
        TrainingRef(**asdict(refs["evaluation"])),
        TrainingRef(**plan["truth"]),
        output_uri=f"gs://{plan['truth']['uri'].split('/')[2]}/lakehouse/vertex-workflows",
        max_rows=256,
    )
    trained = train(config, store)
    if validate_completion(trained) != config:
        raise ValueError("Training completion differs")
    for record in trained["artifacts"].values():
        store.verify(record)
    metadata = json.loads(store.verify(trained["artifacts"]["metadata"]))
    if not metadata["model_reload_verified"]:
        raise ValueError("Model reload verification absent")
    vertex_ref = pin(store, config.root + "/completion.json")
    store.create(
        root(plan) + "/model-verified.json",
        canonical(
            {
                "plan_hash": plan_hash,
                "completion": asdict(vertex_ref),
            }
        ),
    )
    from synthetic_quality_lakehouse.bigquery import load as load_data
    from synthetic_quality_vertex.predictions import load as load_predictions

    receipts = {}
    for split in ("training", "evaluation"):
        receipts[split] = load_data(
            manifests[split], store, client, PROJECT, plan["dataset"], REGION
        )
        store.create(root(plan) + "/published-" + split + ".json", canonical(receipts[split]))
    receipts["predictions"] = load_predictions(
        trained, store, client, PROJECT, plan["dataset"], REGION
    )
    store.create(root(plan) + "/published-predictions.json", canonical(receipts["predictions"]))
    # Recheck sources and artifacts after successful transactional reconciliation.
    for ref in refs.values():
        read_ref(store, ref)
    for record in trained["artifacts"].values():
        store.verify(record)
    if any(r["status"] != "PUBLISHED" for r in receipts.values()):
        raise ValueError("Publication/reconciliation incomplete")
    complete = {
        "format_version": 1,
        "status": "COMPLETE",
        "run_id": plan["run_id"],
        "plan_hash": plan_hash,
        "artifacts_verified": True,
        "reconciled": True,
        "training_rows": trained["training_rows"],
        "evaluation_rows": trained["evaluation_rows"],
        "dataproc": {s: asdict(r) for s, r in refs.items()},
        "vertex": asdict(vertex_ref),
        "publications": receipts,
        "model_sha256": trained["artifacts"]["model"]["sha256"],
        "metrics": trained["artifacts"]["metrics"],
        "verified_objects": [
            *[asdict(ref) for ref in refs.values()],
            asdict(vertex_ref),
            *[
                record
                for manifest in manifests.values()
                for record in manifest["artifacts"].values()
            ],
            *[trained["artifacts"][key] for key in sorted(trained["artifacts"])],
        ],
    }
    store.create(root(plan) + "/completion.json", canonical(complete))
    return complete


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--phase", choices=("training", "evaluation", "vertex"), required=True)
    args = parser.parse_args()
    with open(args.plan) as stream:
        plan = json.load(stream)
    validate(plan)
    if not plan["enabled"]:
        parser.error("Reviewed pilot disabled; no cloud clients created")
    from google.cloud import storage

    from synthetic_quality_lakehouse.storage import GCSObjects

    store = GCSObjects(storage.Client(project=PROJECT))
    if args.phase == "vertex":
        from google.cloud import bigquery

        result = run_vertex(plan, store, BoundedBQ(bigquery.Client(project=PROJECT)))
    else:
        result = run_spark(plan, args.phase, store)
    print(json.dumps({"status": result["status"], "run_id": plan["run_id"], "phase": args.phase}))


if __name__ == "__main__":
    main()
