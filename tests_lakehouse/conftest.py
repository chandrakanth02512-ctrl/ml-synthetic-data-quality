"""Real local Spark, fake object/job boundaries; no GCP credentials required."""

import hashlib
import json

import pytest
from google.api_core.exceptions import PreconditionFailed
from pyspark.sql import SparkSession

from synthetic_quality.data_generator import generate_transactions
from synthetic_quality_lakehouse.config import RunConfig
from synthetic_quality_lakehouse.storage import GCSObjects


@pytest.fixture(autouse=True)
def no_live_clients(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline tests must never construct live clients")

    monkeypatch.setattr("google.cloud.storage.Client", forbidden)
    monkeypatch.setattr("google.cloud.bigquery.Client", forbidden)


@pytest.fixture(scope="session")
def spark():
    session = (
        SparkSession.builder.master("local[2]")
        .appName("idq-offline-tests")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.shuffle.partitions", "2")
        .config("spark.driver.bindAddress", "127.0.0.1")
        .getOrCreate()
    )
    session.sparkContext.setLogLevel("ERROR")
    yield session
    session.stop()


@pytest.fixture
def normal_data():
    data = generate_transactions(16, seed=42, dataset_name="training").transactions
    rows = []
    for row in data.to_dict("records"):
        row["timestamp"] = row["timestamp"].isoformat()
        rows.append(row)
    return ("\n".join(json.dumps(r) for r in rows) + "\n").encode()


class Blob:
    def __init__(self, bucket, name, generation=None):
        self.bucket, self.name = bucket, name
        self.generation = generation

    @property
    def size(self):
        return len(self.bucket.objects[self.name][0])

    def upload_from_string(self, data, **kwargs):
        assert kwargs["if_generation_match"] == 0 and kwargs["retry"] is None
        self.bucket.attempts.append(self.name)
        if self.name in self.bucket.objects:
            raise PreconditionFailed("exists")
        if self.bucket.fail_before == self.name:
            self.bucket.fail_before = None
            raise RuntimeError("injected upload failure")
        self.bucket.counter += 1
        self.bucket.objects[self.name] = data, self.bucket.counter
        if self.bucket.lose_ack == self.name:
            self.bucket.lose_ack = None
            raise RuntimeError("injected lost acknowledgement")

    def download_as_bytes(self, **kwargs):
        assert kwargs["retry"] is None
        data, generation = self.bucket.objects[self.name]
        assert kwargs["if_generation_match"] == generation
        return data


class Bucket:
    def __init__(self):
        self.objects, self.attempts = {}, []
        self.counter, self.fail_before, self.lose_ack = 0, None, None

    def blob(self, name):
        return Blob(self, name)

    def get_blob(self, name, generation=None, **kwargs):
        assert kwargs["retry"] is None
        if name not in self.objects:
            return None
        actual = self.objects[name][1]
        if generation is not None and actual != generation:
            return None
        return Blob(self, name, actual)


class Client:
    def __init__(self):
        self.buckets = {}

    def bucket(self, name):
        return self.buckets.setdefault(name, Bucket())


@pytest.fixture
def objects():
    return GCSObjects(Client())


@pytest.fixture
def source_config(normal_data, objects):
    uri = "gs://offline-data/lakehouse/inputs/source.ndjson"
    record = objects.create(uri, normal_data)
    return RunConfig(
        uri,
        record["generation"],
        hashlib.sha256(normal_data).hexdigest(),
        "gs://offline-data/lakehouse/phase2",
        "pilot-001",
    )


class SharedSession:
    """Driver owns session lifetime in production; share the real session in tests."""

    def __init__(self, session):
        self.session = session

    def __getattr__(self, name):
        return getattr(self.session, name)

    def stop(self):
        pass


@pytest.fixture
def completed(source_config, objects, spark):
    from synthetic_quality_lakehouse.driver import run

    return run(source_config, objects, lambda: SharedSession(spark))
