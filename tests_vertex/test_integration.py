from pyspark.sql import SparkSession

from synthetic_quality_lakehouse.config import RunConfig, canonical
from synthetic_quality_lakehouse.driver import run as dataproc
from synthetic_quality_vertex.config import TrainingConfig
from synthetic_quality_vertex.train import run as train
from tests_lakehouse.conftest import SharedSession
from tests_vertex.conftest import artifact, reference


def test_real_dataproc_contract_to_cpu_training_and_prediction_publication(store, data_pair):
    from synthetic_quality_vertex.predictions import load
    from tests_vertex.test_predictions import client

    spark = (
        SparkSession.builder.master("local[2]")
        .appName("vertex-contract-test")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.shuffle.partitions", "2")
        .config("spark.driver.bindAddress", "127.0.0.1")
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("ERROR")
    refs = []
    try:
        for name, dataset in zip(("train-spark", "eval-spark"), data_pair, strict=True):
            rows = dataset.transactions.to_dict("records")
            for row in rows:
                row["timestamp"] = row["timestamp"].isoformat()
            if name == "train-spark":
                rows.append({**rows[0], "customer_id": None, "transaction_id": "invalid-only"})
            source = reference(
                store,
                "gs://offline-data/lakehouse/inputs/" + name,
                b"".join(canonical(r) + b"\n" for r in rows),
            )
            cfg = RunConfig(
                source.uri,
                source.generation,
                source.sha256,
                "gs://offline-data/lakehouse/phase2",
                name,
            )
            manifest = dataproc(cfg, store, lambda: SharedSession(spark))
            refs.append(reference(store, cfg.root + "/completion.json", canonical(manifest)))
    finally:
        spark.stop()
    labels = data_pair[1].truth.to_dict("records")
    for row in labels:
        row["is_anomaly"] = int(row["is_anomaly"])
    truth = reference(
        store,
        "gs://offline-data/lakehouse/vertex/truth/spark",
        b"".join(canonical(r) + b"\n" for r in labels),
    )
    cfg = TrainingConfig("spark-vertex", *refs, truth, "gs://offline-data/lakehouse/vertex")
    completion = train(cfg, store)
    metadata = artifact(store, completion, "metadata")
    assert metadata["training_quarantined"] == 1
    assert (completion["training_rows"], completion["evaluation_rows"]) == (256, 100)
    assert metadata["evaluation"]["isolation_forest"]["total_actual_anomalies"] == 5
    api = client()
    assert load(completion, store, api, "offline-project", "pilot")["status"] == "PUBLISHED"
    assert len(api.submitted) == 2
