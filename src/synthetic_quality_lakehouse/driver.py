"""Dataproc driver: bounded immutable ingestion, Spark SQL, create-only publication."""

import argparse
import hashlib
import json
from dataclasses import fields

import pyspark
from pyspark.sql import SparkSession

from synthetic_quality_lakehouse.config import RunConfig, canonical
from synthetic_quality_lakehouse.manifest import validate_manifest
from synthetic_quality_lakehouse.source import parse_source


def run(
    config, store, spark_factory=lambda: SparkSession.builder.appName("idq-lakehouse").getOrCreate()
):
    request = config.request(pyspark.__version__)
    fingerprint = hashlib.sha256(canonical(request)).hexdigest()
    store.create(config.root + "/request.json", canonical(request))
    complete = store.optional(config.root + "/completion.json")
    if complete:
        manifest = json.loads(complete[0])
        if manifest.get("request") != request or manifest.get("status") != "COMPLETE":
            raise ValueError("Completion/request mismatch")
        validate_manifest(manifest)
        for item in manifest["artifacts"].values():
            store.verify(item)
        return manifest
    source, _ = store.read(config.source_uri, config.source_generation, config.max_bytes)
    records = parse_source(source, config.source_sha256, config.max_rows)
    from synthetic_quality_lakehouse.spark import transform

    spark = spark_factory()
    output = None
    try:
        output = transform(spark, records, config.run_id, fingerprint)
        artifacts = {}
        for kind in ("clean", "quarantine", "quality", "ledger"):
            frame = getattr(output, kind)
            ordering = "check_name" if kind == "quality" else "source_row_number"
            lines = list(frame.orderBy(ordering).toJSON().toLocalIterator())
            payload = ("\n".join(lines) + ("\n" if lines else "")).encode()
            artifacts[kind] = {
                **store.create(config.root + "/" + kind + ".ndjson", payload),
                "row_count": len(lines),
            }
        manifest = {
            "status": "COMPLETE",
            "request": request,
            "fingerprint": fingerprint,
            "source_rows": len(records),
            "artifacts": artifacts,
        }
        validate_manifest(manifest)
        store.create(config.root + "/completion.json", canonical(manifest))
        return manifest
    finally:
        if output:
            output.classified.unpersist()
        spark.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for f in fields(RunConfig):
        if f.name.startswith("max_"):
            continue
        parser.add_argument(
            "--" + f.name.replace("_", "-"),
            required=True,
            type=int if f.name == "source_generation" else str,
        )
    config = RunConfig(**vars(parser.parse_args()))
    from google.cloud import storage

    from synthetic_quality_lakehouse.storage import GCSObjects

    result = run(config, GCSObjects(storage.Client()))
    print(
        json.dumps(
            {
                "status": result["status"],
                "run_id": config.run_id,
                "source_rows": result["source_rows"],
                "manifest_uri": config.root + "/completion.json",
            }
        )
    )


if __name__ == "__main__":
    main()
