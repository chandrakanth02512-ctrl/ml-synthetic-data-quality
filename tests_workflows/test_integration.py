"""Actual YAML + local Spark + existing sklearn + fake GCS/BigQuery interfaces."""

import json
from pathlib import Path

import yaml
from google.cloud import bigquery as bq
from pyspark.sql import SparkSession

from synthetic_quality_lakehouse.config import canonical
from synthetic_quality_vertex import predictions
from synthetic_quality_workflows import runner
from synthetic_quality_workflows.plan import PROJECT, ROOT, compile_plan
from tests_lakehouse.conftest import SharedSession
from tests_lakehouse.test_bigquery import FakeBQ
from tests_workflows.effects import Effects
from tests_workflows.test_workflow import run


def test_complete_workflow_with_real_spark_training_quarantine_and_replay(plan, store, data_pair):
    for name, generated in zip(("training", "evaluation"), data_pair, strict=True):
        rows = generated.transactions.to_dict("records")
        for row in rows:
            row["timestamp"] = row["timestamp"].isoformat()
        if name == "training":
            rows.append({"transaction_id": "quarantine", "transaction_amount": None})
        plan[name] = store.create(
            ROOT + "/inputs/" + name + ".ndjson", b"".join(canonical(r) + b"\n" for r in rows)
        )
    truth = data_pair[1].truth.to_dict("records")
    for row in truth:
        row["is_anomaly"] = int(row["is_anomaly"])
    plan["truth"] = store.create(
        ROOT + "/inputs/truth.ndjson", b"".join(canonical(r) + b"\n" for r in truth)
    )
    bundle = {"plan": plan, "reference": store.create(ROOT + "/plans/real.json", canonical(plan))}
    compiled = compile_plan(**bundle)
    definition = yaml.safe_load(Path("workflows/pipeline.yaml").read_text())
    definition["main"]["steps"][0]["initialize"]["assign"][0]["p"] = compiled
    effects = Effects(store, compiled)
    api = FakeBQ()
    api.tables = {k.replace("offline-project", PROJECT): v for k, v in api.tables.items()}
    for name, fields in (
        ("anomaly_predictions", predictions.PREDICTIONS),
        ("model_runs", predictions.RUNS),
    ):
        table = bq.Table(PROJECT + ".pilot." + name, schema=predictions.schema(fields))
        table.labels = predictions.LABELS
        api.tables[str(table.reference)] = table
    spark = (
        SparkSession.builder.master("local[2]")
        .appName("workflow-offline")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.shuffle.partitions", "2")
        .config("spark.driver.bindAddress", "127.0.0.1")
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("ERROR")
    try:
        for split in ("training", "evaluation"):
            effects.hooks[split] = lambda split=split: runner.run_spark(
                plan, split, store, lambda: SharedSession(spark)
            )
        effects.hooks["vertex"] = lambda: runner.run_vertex(plan, store, api)
        report, _ = run(definition, effects)
        assert report["status"] == "COMPLETE"
        assert (report["report"]["training_rows"], report["report"]["evaluation_rows"]) == (
            256,
            100,
        )
        manifest = json.loads(store.read(report["report"]["dataproc"]["training"]["uri"])[0])
        assert manifest["artifacts"]["quarantine"]["row_count"] == 1
        assert manifest["source_rows"] == 257
        assert len(api.submitted) == len(set(api.submitted)) == 11
        assert len(report["report"]["verified_objects"]) == 17
        before = list(api.submitted)
        assert run(definition, effects, "resume", "execution-002")[0]["status"] == "COMPLETE"
        assert api.submitted == before and len(effects.submissions) == 3
    finally:
        spark.stop()
