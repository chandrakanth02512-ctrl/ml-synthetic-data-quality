"""Offline integration with real generation/training and a generation-aware GCS fake."""

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from google.api_core.exceptions import Forbidden, PreconditionFailed, ServiceUnavailable
from google.auth.exceptions import DefaultCredentialsError, RefreshError

from synthetic_quality import cloud_job
from synthetic_quality.cloud_config import CloudConfig
from synthetic_quality.gcs import RETRY, TIMEOUT, GCSStore, RunConflictError


class FakeBlob:
    def __init__(self, bucket, name, generation=None):
        self.bucket = bucket
        self.name = name
        self.generation = generation

    def upload_from_string(self, data, **kwargs):
        assert kwargs["if_generation_match"] == 0
        assert kwargs["retry"] is RETRY and kwargs["timeout"] == TIMEOUT
        assert kwargs["checksum"] is None
        assert kwargs["content_type"] in {
            "application/json",
            "text/csv",
            "application/octet-stream",
        }
        self.bucket.attempts.append(self.name)
        if self.name in self.bucket.objects:
            raise PreconditionFailed("already exists")
        if self.name == self.bucket.fail_before:
            raise ServiceUnavailable("injected exhausted retry budget")
        self.bucket.counter += 1
        self.bucket.objects[self.name] = (data, self.bucket.counter)
        self.bucket.writes.append(self.name)
        if self.name == self.bucket.fail_after:
            raise ServiceUnavailable("commit succeeded but ACK lost; retries exhausted")
        if self.name == self.bucket.lost_ack:
            raise PreconditionFailed("retry after lost ACK sees existing generation")
        if self.name == self.bucket.corrupt:
            self.bucket.objects[self.name] = (b"corrupt", self.bucket.counter)

    def download_as_bytes(self, **kwargs):
        assert kwargs["if_generation_match"] == self.generation
        assert kwargs["retry"] is RETRY and kwargs["timeout"] == TIMEOUT
        assert kwargs["checksum"] == "auto"
        data, generation = self.bucket.objects[self.name]
        if generation != self.generation:
            raise PreconditionFailed("generation changed")
        return data


class FakeBucket:
    def __init__(self):
        self.objects = {}
        self.counter = 0
        self.attempts = []
        self.writes = []
        self.fail_before = self.fail_after = self.lost_ack = self.corrupt = None

    def blob(self, name):
        return FakeBlob(self, name)

    def get_blob(self, name, **kwargs):
        assert kwargs == {"timeout": TIMEOUT, "retry": RETRY}
        if name not in self.objects:
            return None
        return FakeBlob(self, name, self.objects[name][1])


@pytest.fixture(autouse=True)
def forbid_live_gcs(monkeypatch):
    monkeypatch.setattr(
        cloud_job.storage, "Client", Mock(side_effect=AssertionError("No live GCP"))
    )


@pytest.fixture
def config():
    return CloudConfig(
        "sample-project", "test-bucket", "smoke-001", training_rows=256, evaluation_rows=100
    )


@pytest.fixture
def bucket():
    return FakeBucket()


@pytest.fixture
def client(bucket):
    return Mock(bucket=Mock(return_value=bucket))


@pytest.fixture
def completed(config, client):
    return cloud_job.run_cloud_job(config, client=client)


def test_full_pipeline_artifacts_manifest_and_temp_cleanup(config, client, bucket, monkeypatch):
    generated_directories = []
    pipeline = cloud_job.run_pipeline

    def record_directory(directory, settings):
        generated_directories.append(Path(directory))
        return pipeline(directory, settings)

    monkeypatch.setattr(cloud_job, "run_pipeline", record_directory)
    result = cloud_job.run_cloud_job(config, client=client)
    assert result["status"] == "complete"
    assert len(result["artifacts"]) == 12
    assert len(bucket.objects) == 14
    assert bucket.writes[-1].endswith("/completion.json")
    assert bucket.writes[0].endswith("/request.json")
    assert all(not path.exists() for path in generated_directories)
    for local, record in result["artifacts"].items():
        assert record["name"] == config.object_name(cloud_job.ARTIFACTS[local], Path(local).name)
        assert record["size_bytes"] > 0
    quality = json.loads(
        bucket.objects[config.object_name("quality-reports", "data_quality.json")][0]
    )
    assert quality["training"]["passed"] and quality["evaluation"]["passed"]
    assert quality["evaluation"]["row_count"] == 100
    model_metadata = json.loads(
        bucket.objects[config.object_name("models", "model_metadata.json")][0]
    )
    assert model_metadata["model_reload_verified"]


def test_completed_retry_verifies_without_regeneration(
    completed, config, client, bucket, monkeypatch
):
    original = dict(bucket.objects)
    monkeypatch.setattr(cloud_job, "run_pipeline", Mock(side_effect=AssertionError("must not run")))
    assert cloud_job.run_cloud_job(config, client=client) == completed
    assert bucket.objects == original
    assert len(bucket.writes) == 14


@pytest.mark.parametrize("field,value", [("training_rows", 257), ("evaluation_seed", 124)])
def test_run_id_cannot_be_reused_for_other_configuration(
    completed, config, client, bucket, monkeypatch, field, value
):
    original = dict(bucket.objects)
    monkeypatch.setattr(cloud_job, "run_pipeline", Mock(side_effect=AssertionError("must not run")))
    with pytest.raises(RunConflictError, match="request.json"):
        cloud_job.run_cloud_job(replace(config, **{field: value}), client=client)
    assert bucket.objects == original


def test_changed_runtime_rejected(completed, config, client, monkeypatch):
    monkeypatch.setattr(cloud_job, "version", lambda name: "different-version")
    with pytest.raises(RunConflictError, match="request.json"):
        cloud_job.run_cloud_job(config, client=client)


@pytest.mark.parametrize("failure", ["fail_before", "fail_after", "lost_ack"])
@pytest.mark.parametrize("target", ["artifact", "completion"])
def test_partial_failures_and_lost_ack_recover_without_overwrite(
    config, client, bucket, failure, target
):
    name = (
        config.object_name("models", "isolation_forest.joblib")
        if target == "artifact"
        else (config.object_name("run-manifests", "completion.json"))
    )
    setattr(bucket, failure, name)
    if failure == "lost_ack":
        cloud_job.run_cloud_job(config, client=client)
    else:
        with pytest.raises(ServiceUnavailable):
            cloud_job.run_cloud_job(config, client=client)
        if target == "artifact" or failure == "fail_before":
            assert config.object_name("run-manifests", "completion.json") not in bucket.objects
    original = dict(bucket.objects)
    setattr(bucket, failure, None)
    result = cloud_job.run_cloud_job(config, client=client)
    assert result["status"] == "complete"
    assert all(bucket.objects[name] == value for name, value in original.items())
    assert len(bucket.writes) == len(set(bucket.writes)) == 14


def test_pipeline_failure_cleans_temp_and_never_commits(config, client, bucket, monkeypatch):
    directories = []

    def broken(directory, settings):
        directories.append(Path(directory))
        raise ValueError("Data quality failed")

    monkeypatch.setattr(cloud_job, "run_pipeline", broken)
    with pytest.raises(ValueError, match="Data quality failed"):
        cloud_job.run_cloud_job(config, client=client)
    assert all(not path.exists() for path in directories)
    assert list(bucket.objects) == [config.object_name("run-manifests", "request.json")]


def test_conflicting_partial_artifact_is_preserved(config, client, bucket):
    name = config.object_name("raw-data", "training.csv")
    bucket.objects[name] = (b"other content", 100)
    with pytest.raises(RunConflictError, match="training.csv"):
        cloud_job.run_cloud_job(config, client=client)
    assert bucket.objects[name] == (b"other content", 100)
    assert config.object_name("run-manifests", "completion.json") not in bucket.objects


@pytest.mark.parametrize("damage", ["missing", "bytes", "generation"])
def test_completed_artifact_damage_fails_closed(completed, config, client, bucket, damage):
    name = config.object_name("raw-data", "training.csv")
    data, generation = bucket.objects.pop(name)
    if damage == "bytes":
        bucket.objects[name] = (b"modified", generation)
    elif damage == "generation":
        bucket.objects[name] = (data, generation + 100)
    with pytest.raises(RunConflictError, match="missing or changed"):
        cloud_job.run_cloud_job(config, client=client)


@pytest.mark.parametrize(
    "payload", [b"invalid json", b"\xff", b"[]", b"{}", b'{"status": "incomplete"}']
)
def test_malformed_completion_fails_closed(completed, config, client, bucket, payload):
    name = config.object_name("run-manifests", "completion.json")
    bucket.objects[name] = (payload, 100)
    with pytest.raises(RunConflictError, match="manifest"):
        cloud_job.run_cloud_job(config, client=client)


@pytest.mark.parametrize("alteration", ["request", "missing-artifact", "artifact-type", "path"])
def test_manifest_mismatch_fails_closed(completed, config, client, bucket, alteration):
    if alteration == "request":
        completed["request"] = {}
    elif alteration == "missing-artifact":
        completed["artifacts"].pop("data/training.csv")
    elif alteration == "artifact-type":
        completed["artifacts"] = []
    else:
        completed["artifacts"]["data/training.csv"]["name"] = "../../external"
    name = config.object_name("run-manifests", "completion.json")
    bucket.objects[name] = (cloud_job.json_bytes(completed), 100)
    with pytest.raises(RunConflictError):
        cloud_job.run_cloud_job(config, client=client)
    assert "../../external" not in bucket.attempts


def test_new_run_id_gets_own_namespace(completed, config, client, bucket):
    cloud_job.run_cloud_job(replace(config, run_id="smoke-002"), client=client)
    assert len(bucket.objects) == 28


def test_upload_corruption_is_detected_without_deletion(config, client, bucket):
    bucket.corrupt = config.object_name("raw-data", "training.csv")
    with pytest.raises(RunConflictError, match="training.csv"):
        cloud_job.run_cloud_job(config, client=client)
    assert bucket.objects[bucket.corrupt][0] == b"corrupt"
    assert config.object_name("run-manifests", "completion.json") not in bucket.objects


def test_disappearing_upload_fails_closed(client, bucket, monkeypatch):
    monkeypatch.setattr(bucket, "get_blob", lambda *args, **kwargs: None)
    with pytest.raises(RunConflictError):
        GCSStore(client, "test-bucket").create("safe/file.json", b"{}", "application/json")


def test_changed_generation_during_download_fails(client, bucket, monkeypatch):
    bucket.objects["safe/file.json"] = (b"{}", 2)
    monkeypatch.setattr(bucket, "get_blob", lambda *a, **k: FakeBlob(bucket, "safe/file.json", 1))
    with pytest.raises(PreconditionFailed):
        GCSStore(client, "test-bucket").read("safe/file.json")


def test_storage_rejects_unsafe_key_before_sdk_call(client, bucket):
    store = GCSStore(client, "test-bucket")
    with pytest.raises(ValueError):
        store.create("../unsafe", b"{}", "application/json")
    with pytest.raises(ValueError):
        store.read("/unsafe")
    assert not bucket.attempts


def test_adc_client_uses_explicit_project(config, client, monkeypatch):
    factory = Mock(return_value=client)
    monkeypatch.setattr(cloud_job.storage, "Client", factory)
    cloud_job.run_cloud_job(config)
    factory.assert_called_once_with(project="sample-project")


@pytest.mark.parametrize(
    "error,message",
    [
        (DefaultCredentialsError("secret detail"), "Cloud authentication failed"),
        (RefreshError("secret detail"), "Cloud authentication failed"),
        (Forbidden("secret detail"), "GCS operation failed (Forbidden)"),
        (ServiceUnavailable("secret detail"), "GCS operation failed (ServiceUnavailable)"),
        (RunConflictError("conflict"), "Cloud job failed: conflict"),
        (OSError("disk full"), "Cloud job failed: disk full"),
    ],
)
def test_cli_clear_failures(config, monkeypatch, capsys, error, message):
    monkeypatch.setattr(cloud_job, "parse_config", lambda argv: config)
    monkeypatch.setattr(cloud_job.storage, "Client", Mock(side_effect=error))
    with pytest.raises(SystemExit) as result:
        cloud_job.main([])
    assert result.value.code == 1
    stderr = capsys.readouterr().err
    assert message in stderr
    assert "secret detail" not in stderr


def test_cli_success(config, client, monkeypatch, capsys):
    monkeypatch.setattr(cloud_job, "parse_config", lambda argv: config)
    monkeypatch.setattr(cloud_job.storage, "Client", Mock(return_value=client))
    cloud_job.main([])
    assert "complete; gs://test-bucket/" in capsys.readouterr().out
