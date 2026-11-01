import copy

import pytest
from google.cloud import bigquery as bq
from sqlfluff import parse
from sqlfluff.api.simple import APIParsingError

from synthetic_quality_vertex import predictions
from synthetic_quality_vertex.prediction_sql import PUBLISH
from tests_lakehouse.test_bigquery import FakeBQ


def client():
    result = FakeBQ()
    for name, fields in (
        ("anomaly_predictions", predictions.PREDICTIONS),
        ("model_runs", predictions.RUNS),
    ):
        table = bq.Table("offline-project.pilot." + name, schema=predictions.schema(fields))
        table.labels = predictions.LABELS
        result.tables[str(table.reference)] = table
    return result


def test_prediction_loader_reuses_existing_fixed_id_job_recovery(trained, store):
    api = client()
    first = predictions.load(trained, store, api, "offline-project", "pilot")
    assert first["status"] == "PUBLISHED"
    assert predictions.load(trained, store, api, "offline-project", "pilot") == first
    assert len(api.submitted) == 2
    load_job = next(j for j in api.jobs.values() if "load" in j.configuration)
    assert load_job.configuration["load"]["writeDisposition"] == "WRITE_EMPTY"


@pytest.mark.parametrize("failure", ["fail_load", "fail_query", "lost_load_ack"])
def test_failed_and_ambiguous_publication_is_not_duplicated(trained, store, failure):
    api = client()
    setattr(api, failure, True)
    with pytest.raises((RuntimeError, TimeoutError)):
        predictions.load(trained, store, api, "offline-project", "pilot")
    count = len(api.submitted)
    if failure == "lost_load_ack":
        assert (
            predictions.load(trained, store, api, "offline-project", "pilot")["status"]
            == "PUBLISHED"
        )
        assert len(api.submitted) == 2
    else:
        with pytest.raises(RuntimeError):
            predictions.load(trained, store, api, "offline-project", "pilot")
        assert len(api.submitted) == count


def test_wrong_schema_region_and_corrupt_artifact_prevent_jobs(trained, store):
    api = client()
    api.location = "US"
    with pytest.raises(ValueError):
        predictions.load(trained, store, api, "offline-project", "pilot")
    api.location = "asia-south1"
    api.tables["offline-project.pilot.anomaly_predictions"].labels = {}
    with pytest.raises(ValueError):
        predictions.load(trained, store, api, "offline-project", "pilot")
    broken = copy.deepcopy(trained)
    broken["artifacts"]["predictions"]["sha256"] = "0" * 64
    with pytest.raises(ValueError):
        predictions.load(broken, store, client(), "offline-project", "pilot")
    assert not api.submitted


def test_full_publication_sql_parses_and_bad_nested_insert_is_rejected():
    sql = PUBLISH.format(dataset="offline-project.pilot", stage="offline-project.pilot.stage")
    assert parse(sql, dialect="bigquery")["file"]
    assert parse(predictions.ddl("offline-project", "pilot"), dialect="bigquery")["file"]
    with pytest.raises(APIParsingError):
        parse(sql.replace("END IF;", ""), dialect="bigquery")
    assert "EXCEPT DISTINCT" in sql
    assert "GREATEST(rule_based_prediction, isolation_forest_prediction)" in sql


def test_ddl_and_load_gate_do_not_authenticate(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["predictions", "ddl", "--project", "offline-project"])
    predictions.main()
    assert "CREATE TABLE IF NOT EXISTS" in capsys.readouterr().out
    monkeypatch.setattr("sys.argv", ["predictions", "load", "--project", "offline-project"])
    with pytest.raises(SystemExit):
        predictions.main()


def test_approved_cli_publication_uses_injected_clients_only(
    trained, config, store, monkeypatch, capsys
):
    api = client()
    monkeypatch.setattr(
        "sys.argv",
        [
            "predictions",
            "load",
            "--project",
            "offline-project",
            "--dataset",
            "pilot",
            "--manifest-uri",
            config.root + "/completion.json",
            "--approve-publication",
        ],
    )
    monkeypatch.setattr("google.cloud.storage.Client", lambda **kwargs: store.client)
    monkeypatch.setattr("google.cloud.bigquery.Client", lambda **kwargs: api)
    predictions.main()
    assert '"PUBLISHED"' in capsys.readouterr().out


def test_existing_stage_creation_race_preserves_matching_table(trained, store):
    from google.api_core.exceptions import Conflict

    api = client()
    original = api.create_table

    def race(table, **kwargs):
        original(table, **kwargs)
        raise Conflict("another creator acknowledged the same table")

    api.create_table = race
    assert (
        predictions.load(trained, store, api, "offline-project", "pilot")["status"] == "PUBLISHED"
    )
