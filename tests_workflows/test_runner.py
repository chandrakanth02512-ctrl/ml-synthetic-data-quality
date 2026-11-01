import copy
import json
from unittest.mock import Mock

import pytest
from google.cloud import bigquery as bq

from synthetic_quality_lakehouse.config import canonical
from synthetic_quality_vertex import predictions
from synthetic_quality_workflows import runner
from synthetic_quality_workflows.plan import PROJECT, REGION, root
from tests_lakehouse.test_bigquery import FakeBQ, FakeJob
from tests_workflows.conftest import manifests_for_plan


def bq_client():
    api = FakeBQ()
    api.tables = {k.replace("offline-project", PROJECT): v for k, v in api.tables.items()}
    for name, fields in (
        ("anomaly_predictions", predictions.PREDICTIONS),
        ("model_runs", predictions.RUNS),
    ):
        table = bq.Table(PROJECT + ".pilot." + name, schema=predictions.schema(fields))
        table.labels = predictions.LABELS
        api.tables[str(table.reference)] = table
    return api


@pytest.fixture
def prepared(plan, store, data_pair):
    return manifests_for_plan(plan, store, data_pair)[0]


def test_real_training_and_both_existing_loaders_replay_without_duplicate_jobs(prepared, store):
    api = bq_client()
    report = runner.run_vertex(prepared, store, api)
    assert report["status"] == "COMPLETE" and report["reconciled"] is True
    assert (report["training_rows"], report["evaluation_rows"]) == (256, 100)
    assert len(api.submitted) == len(set(api.submitted)) == 10
    before = list(api.submitted)
    assert runner.run_vertex(prepared, store, api) == report
    assert api.submitted == before
    for suffix in (
        "model-verified",
        "published-training",
        "published-evaluation",
        "published-predictions",
        "completion",
    ):
        assert store.read(root(prepared) + "/" + suffix + ".json")


@pytest.mark.parametrize("failure", ["fail_load", "fail_query", "lost_load_ack"])
def test_bigquery_failure_and_lost_ack_never_duplicate_or_report_complete(prepared, store, failure):
    api = bq_client()
    setattr(api, failure, True)
    with pytest.raises((RuntimeError, TimeoutError)):
        runner.run_vertex(prepared, store, api)
    assert not store.optional(root(prepared) + "/completion.json")
    before = list(api.submitted)
    if failure == "lost_load_ack":
        assert runner.run_vertex(prepared, store, api)["status"] == "COMPLETE"
        assert len(api.submitted) == len(set(api.submitted)) == 10
    else:
        with pytest.raises(RuntimeError):
            runner.run_vertex(prepared, store, api)
        assert api.submitted == before


@pytest.mark.parametrize("fault", ["count", "schema", "truth", "hash"])
def test_invalid_inputs_stop_before_publication(prepared, store, fault):
    api = bq_client()
    if fault == "count":
        prepared["training_rows"] = 255
    elif fault == "truth":
        uri = root(prepared) + "/wrong-truth.ndjson"
        prepared["truth"] = store.create(uri, b'{"transaction_id":"extra","is_anomaly":1}\n')
    elif fault == "schema":
        uri = runner.processing(prepared, "training").root + "/completion.json"
        bucket = store.client.bucket(uri.split("/")[2])
        path = "/".join(uri.split("/")[3:])
        data, generation = bucket.objects[path]
        value = json.loads(data)
        value["artifacts"]["clean"]["row_count"] -= 1
        bucket.objects[path] = canonical(value), generation
    else:
        prepared["truth"]["sha256"] = "0" * 64
    with pytest.raises((ValueError, AssertionError)):
        runner.run_vertex(prepared, store, api)
    assert not api.submitted
    assert not store.optional(root(prepared) + "/completion.json")


def test_training_failure_and_corrupt_model_stop_before_bigquery(prepared, store, monkeypatch):
    api = bq_client()

    def failed(*args):
        raise ValueError("injected model failure")

    with monkeypatch.context() as context:
        context.setattr("synthetic_quality_vertex.train.run", failed)
        with pytest.raises(ValueError, match="model failure"):
            runner.run_vertex(prepared, store, api)
    assert not api.submitted
    report = runner.run_vertex(prepared, store, api)
    trained = json.loads(store.read(report["vertex"]["uri"])[0])
    uri = trained["artifacts"]["model"]["uri"]
    bucket = store.client.bucket(uri.split("/")[2])
    path = "/".join(uri.split("/")[3:])
    data, generation = bucket.objects[path]
    bucket.objects[path] = data + b"corruption", generation
    before = list(api.submitted)
    with pytest.raises(ValueError, match="changed"):
        runner.run_vertex(prepared, store, api)
    assert api.submitted == before


def test_partial_receipt_failure_recovers_without_duplicate_publication(prepared, store):
    api = bq_client()
    bucket = store.client.bucket(root(prepared).split("/")[2])
    path = "/".join(root(prepared).split("/")[3:]) + "/published-training.json"
    bucket.lose_ack = path
    with pytest.raises(RuntimeError, match="lost acknowledgement"):
        runner.run_vertex(prepared, store, api)
    assert len(api.submitted) == 4
    assert runner.run_vertex(prepared, store, api)["status"] == "COMPLETE"
    assert len(api.submitted) == len(set(api.submitted)) == 10


def test_publication_deadline_has_no_job_retry_or_unbounded_poll():
    now = [10.0]
    client = Mock()
    job = Mock()
    job.to_api_repr.return_value = {"configuration": {"query": {"query": "SELECT 1"}}}
    client.get_job.return_value = job
    proxy = runner.BoundedBQ(client, seconds=20, clock=lambda: now[0])
    got = proxy.get_job("fixed", retry=None, location=REGION)
    client.get_job.assert_called_once_with("fixed", retry=None, location=REGION, timeout=20)
    got.result(retry=None)
    job.result.assert_called_once_with(retry=None, job_retry=None, timeout=20)
    now[0] = 31
    with pytest.raises(TimeoutError):
        got.result(retry=None)
    with pytest.raises(TimeoutError):
        proxy.query("SELECT 1")
    client.query.assert_not_called()
    with pytest.raises(AttributeError):
        proxy.delete_dataset("anything")


def test_bounded_load_poll_does_not_pass_query_only_argument():
    job = Mock()
    job.to_api_repr.return_value = {"configuration": {"load": {"sourceUris": ["gs://x/y"]}}}
    runner.BoundedJob(job, 20, lambda: 10).result()
    job.result.assert_called_once_with(retry=None, timeout=10)


def test_disabled_runner_and_cli_do_not_authenticate(plan, store, tmp_path, monkeypatch):
    plan["enabled"] = False
    with pytest.raises(ValueError, match="disabled"):
        runner.run_vertex(plan, store, bq_client())
    path = tmp_path / "disabled.json"
    path.write_bytes(canonical(plan))
    monkeypatch.setattr("sys.argv", ["runner", "--plan", str(path), "--phase", "vertex"])
    with pytest.raises(SystemExit):
        runner.main()


def test_config_conflict_and_reconciliation_failure_never_complete(prepared, store, monkeypatch):
    api = bq_client()

    def fail_reconcile(*args, **kwargs):
        raise ValueError("source-to-target reconciliation failed")

    monkeypatch.setattr("synthetic_quality_vertex.predictions.load", fail_reconcile)
    with pytest.raises(ValueError, match="reconciliation"):
        runner.run_vertex(prepared, store, api)
    assert not store.optional(root(prepared) + "/completion.json")
    before = list(api.submitted)
    changed = copy.deepcopy(prepared)
    changed["poll_limit"] += 1
    with pytest.raises(ValueError, match="conflict"):
        runner.run_vertex(changed, store, api)
    assert api.submitted == before


def test_bounded_adapter_uses_existing_job_configuration_comparison():
    from synthetic_quality_lakehouse.bigquery import job_once

    job = FakeJob("fixed", {"load": {"sourceUris": ["gs://other/source"]}})
    api = Mock()
    api.get_job.return_value = job
    with pytest.raises(ValueError, match="configuration"):
        job_once(
            runner.BoundedBQ(api),
            "fixed",
            REGION,
            {"load": {"sourceUris": ["gs://planned/source"]}},
            Mock(),
        )


def test_successful_runtime_cli_uses_bounded_existing_loaders(
    prepared, store, tmp_path, monkeypatch, capsys
):
    path = tmp_path / "plan.json"
    path.write_bytes(canonical(prepared))
    api = bq_client()
    original_create = api.create_table

    def bounded_create(table, **kwargs):
        assert 0 < kwargs.pop("timeout") <= 30
        return original_create(table, **kwargs)

    monkeypatch.setattr(api, "create_table", bounded_create)
    original = FakeJob.result

    def bounded_result(self, **kwargs):
        assert 0 < kwargs.pop("timeout") <= 240
        if "query" in self.configuration:
            assert kwargs.pop("job_retry") is None
        return original(self, **kwargs)

    monkeypatch.setattr(FakeJob, "result", bounded_result)
    monkeypatch.setattr("google.cloud.storage.Client", lambda **kwargs: store.client)
    monkeypatch.setattr("google.cloud.bigquery.Client", lambda **kwargs: api)
    monkeypatch.setattr("sys.argv", ["runner", "--plan", str(path), "--phase", "vertex"])
    runner.main()
    assert '"status": "COMPLETE"' in capsys.readouterr().out
    assert len(api.submitted) == 10


@pytest.mark.parametrize("fault", ["reload", "completion", "publication"])
def test_untrusted_worker_completions_stop_without_final_report(
    prepared, store, monkeypatch, fault
):
    from synthetic_quality_vertex.train import run as train

    original_train = train

    def changed(config, objects):
        manifest = original_train(config, objects)
        if fault == "completion":
            from dataclasses import replace

            manifest["request"]["config"] = replace(config, run_id="other-run").to_dict()
        return manifest

    if fault == "reload":
        original_verify = store.verify

        def unverified(record):
            data = original_verify(record)
            if record["uri"].endswith("model_metadata.json"):
                value = json.loads(data)
                value["model_reload_verified"] = False
                return canonical(value)
            return data

        monkeypatch.setattr(store, "verify", unverified)
    elif fault == "completion":
        monkeypatch.setattr("synthetic_quality_vertex.train.run", changed)
    else:
        monkeypatch.setattr(
            "synthetic_quality_vertex.predictions.load", lambda *args: {"status": "FAILED"}
        )
    with pytest.raises(ValueError):
        runner.run_vertex(prepared, store, bq_client())
    assert not store.optional(root(prepared) + "/completion.json")
