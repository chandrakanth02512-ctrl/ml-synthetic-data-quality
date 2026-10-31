import copy
from types import SimpleNamespace
from unittest.mock import Mock

import duckdb
import pytest
import sqlglot
from google.api_core.exceptions import Conflict, NotFound
from google.cloud import bigquery as bq

from synthetic_quality_lakehouse import bigquery as loader
from synthetic_quality_lakehouse.schemas import TABLES, ddl


class FakeJob:
    def __init__(self, job_id, configuration, error=None):
        self.job_id, self.configuration, self.error = job_id, configuration, error

    def to_api_repr(self):
        return {"configuration": self.configuration}

    def result(self, **kwargs):
        assert kwargs == {"retry": None}
        if self.error:
            raise self.error
        return self


class FakeBQ:
    def __init__(self):
        self.tables, self.jobs, self.submitted = {}, {}, []
        self.lost_load_ack = False
        self.fail_load = False
        self.fail_query = False
        self.location = "asia-south1"
        for kind, name in TABLES.items():
            table = bq.Table("offline-project.pilot." + name, schema=loader.schema(kind))
            table.labels = loader.LABELS
            table._properties["numRows"] = "0"
            self.tables[str(table.reference)] = table

    def get_dataset(self, *args, **kwargs):
        return SimpleNamespace(location=self.location)

    def get_table(self, table_id, **kwargs):
        if table_id not in self.tables:
            raise NotFound("missing")
        return self.tables[table_id]

    def create_table(self, table, **kwargs):
        assert kwargs == {"retry": None}
        key = str(table.reference)
        if key in self.tables:
            raise Conflict("exists")
        table._properties["numRows"] = "0"
        self.tables[key] = table
        return table

    def get_job(self, job_id, **kwargs):
        if job_id not in self.jobs:
            raise NotFound("missing")
        return self.jobs[job_id]

    def load_table_from_uri(self, uri, table_id, *, job_config, job_id, **kwargs):
        assert kwargs["retry"] is None
        config = job_config.to_api_repr()
        config["load"].update(
            {
                "sourceUris": [uri],
                "destinationTable": bq.TableReference.from_string(table_id).to_api_repr(),
            }
        )
        job = FakeJob(job_id, config, RuntimeError("failed load") if self.fail_load else None)
        self.jobs[job_id] = job
        self.submitted.append(job_id)
        if self.lost_load_ack:
            self.lost_load_ack = False
            raise TimeoutError("lost acknowledgement")
        return job

    def query(self, query, *, job_config, job_id, **kwargs):
        assert kwargs["retry"] is None and kwargs["job_retry"] is None
        config = job_config.to_api_repr()
        config["query"]["query"] = query
        self.submitted.append(job_id)
        job = FakeJob(
            job_id, config, RuntimeError("failed publication") if self.fail_query else None
        )
        self.jobs[job_id] = job
        return job


def publish(manifest, objects, client):
    return loader.load(manifest, objects, client, "offline-project", "pilot")


def test_load_and_replay_submit_each_job_at_most_once(completed, objects):
    client = FakeBQ()
    first = publish(completed, objects, client)
    before = list(client.submitted)
    assert publish(completed, objects, client) == first
    assert before == client.submitted
    assert len(before) == 4  # clean, quality, ledger, publication; empty quarantine skips load.
    assert first["status"] == "PUBLISHED"
    assert all(
        "WRITE_EMPTY" == j.configuration["load"]["writeDisposition"]
        for j in client.jobs.values()
        if "load" in j.configuration
    )


def test_interrupted_load_is_reconciled_by_fixed_job_id(completed, objects):
    client = FakeBQ()
    client.lost_load_ack = True
    with pytest.raises(TimeoutError):
        publish(completed, objects, client)
    assert len(client.submitted) == 1
    publish(completed, objects, client)
    assert len(client.submitted) == len(set(client.submitted)) == 4


@pytest.mark.parametrize("failure", ["fail_load", "fail_query"])
def test_failed_jobs_stop_without_replacement(completed, objects, failure):
    client = FakeBQ()
    setattr(client, failure, True)
    with pytest.raises(RuntimeError, match="failed"):
        publish(completed, objects, client)
    count = len(client.submitted)
    with pytest.raises(RuntimeError, match="failed"):
        publish(completed, objects, client)
    assert len(client.submitted) == count


def test_table_drift_and_wrong_location_stop_before_jobs(completed, objects):
    client = FakeBQ()
    client.location = "US"
    with pytest.raises(ValueError, match="location"):
        publish(completed, objects, client)
    client.location = "asia-south1"
    client.tables["offline-project.pilot.clean_transactions"].labels = {"unrelated": "resource"}
    with pytest.raises(ValueError, match="ownership"):
        publish(completed, objects, client)
    assert not client.submitted


def test_job_collision_and_modified_gcs_objects_block_publish(completed, objects):
    client = FakeBQ()
    publish(completed, objects, client)
    next(j for j in client.jobs.values() if "load" in j.configuration).configuration["load"][
        "sourceUris"
    ] = ["gs://unrelated/source"]
    with pytest.raises(ValueError, match="configuration"):
        publish(completed, objects, client)
    broken = copy.deepcopy(completed)
    broken["artifacts"]["clean"]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="changed"):
        publish(broken, objects, FakeBQ())


def test_conflict_reconciles_existing_job_without_duplicate_submission():
    job = FakeJob("same-id", {"load": {"x": 1}})
    client = Mock()
    client.get_job.side_effect = [NotFound("initially absent"), job]
    submit = Mock(side_effect=Conflict("another operator acknowledged the same job"))
    assert loader.job_once(client, "same-id", "asia-south1", {"load": {"x": 1}}, submit) is job
    submit.assert_called_once()


def test_bigquery_ddl_and_publication_parse_in_bigquery_dialect():
    stages = {
        k: "offline-project.pilot.stg_" + k for k in ("clean", "quarantine", "quality", "ledger")
    }
    query = loader.render_sql("offline-project.pilot", stages)
    statements = sqlglot.parse(query, read="bigquery", error_level="RAISE")
    assert statements and "BEGIN TRANSACTION" in query and "COMMIT TRANSACTION" in query
    assert "ASSERT" in query and "IF NOT EXISTS" in query
    assert not any(s in query for s in ("DELETE FROM", "TRUNCATE", "DROP TABLE"))
    assert len(sqlglot.parse(ddl("offline-project.pilot"), read="bigquery")) == 4


@pytest.mark.parametrize(
    "fault", [None, "missing", "extra", "duplicate", "hash", "amount", "route"]
)
def test_actual_reconciliation_sql_detects_row_loss_duplication_and_changes(fault):
    # Execute the checked-in SQL relational logic locally. This is not BigQuery execution.
    connection = duckdb.connect()
    try:
        connection.execute(
            "CREATE TABLE ledger (run_id VARCHAR, source_row_id VARCHAR, "
            "source_record_sha256 VARCHAR, source_fingerprint VARCHAR, "
            "disposition VARCHAR, amount_usd DOUBLE)"
        )
        connection.execute("INSERT INTO ledger VALUES ('pilot', 'a', 'hash', 'fp', 'clean', 10)")
        connection.execute(
            "CREATE TABLE clean_transactions (run_id VARCHAR, source_row_id VARCHAR, "
            "source_record_sha256 VARCHAR, source_fingerprint VARCHAR, "
            "transaction_amount_usd DOUBLE)"
        )
        connection.execute("INSERT INTO clean_transactions VALUES ('pilot','a','hash','fp',10)")
        connection.execute(
            "CREATE TABLE quarantined_records (run_id VARCHAR, source_row_id VARCHAR, "
            "source_record_sha256 VARCHAR, source_fingerprint VARCHAR)"
        )
        if fault == "missing":
            connection.execute("DELETE FROM clean_transactions")
        elif fault in {"extra", "duplicate"}:
            connection.execute("INSERT INTO clean_transactions SELECT * FROM clean_transactions")
            if fault == "extra":
                connection.execute("UPDATE clean_transactions SET source_row_id='b'")
        elif fault == "hash":
            connection.execute("UPDATE clean_transactions SET source_record_sha256='modified'")
        elif fault == "amount":
            connection.execute("UPDATE clean_transactions SET transaction_amount_usd=11")
        elif fault == "route":
            connection.execute("UPDATE ledger SET disposition='quarantine'")
        sql = loader.sql_text("reconcile.sql").format(dataset="main", ledger="ledger")
        local = sqlglot.transpile(sql, read="bigquery", write="duckdb")[0]
        result = connection.execute(local, {"run_id": "pilot"}).fetchone()
        source, target, source_dupes, target_dupes, mismatches = result
        passed = source == target == 1 and source_dupes == target_dupes == mismatches == 0
        assert passed is (fault is None)
    finally:
        connection.close()


def test_render_rejects_sql_identifier_injection():
    with pytest.raises(ValueError):
        loader.render_sql("offline-project.pilot; DROP SCHEMA x", {})


def test_service_normalized_schema_aliases_do_not_cause_false_job_conflicts():
    existing = FakeJob(
        "same-id",
        {
            "load": {
                "schema": {"fields": [{"name": "count", "type": "INTEGER", "mode": "REQUIRED"}]}
            }
        },
    )
    client = Mock()
    client.get_job.return_value = existing
    submit = Mock(side_effect=AssertionError("Existing job must not be resubmitted"))
    expected = {
        "load": {"schema": {"fields": [{"name": "count", "type": "INT64", "mode": "REQUIRED"}]}}
    }
    assert loader.job_once(client, "same-id", "asia-south1", expected, submit) is existing
    submit.assert_not_called()


def test_cli_load_requires_explicit_approval_before_clients(monkeypatch):
    monkeypatch.setattr("sys.argv", ["bigquery", "load", "--project", "offline-project"])
    with pytest.raises(SystemExit):
        loader.main()


def test_all_quarantined_source_skips_empty_clean_load(source_config, objects, spark):
    import hashlib
    from dataclasses import replace

    from conftest import SharedSession

    from synthetic_quality_lakehouse.driver import run

    data = b"{}\n"
    obj = objects.create("gs://offline-data/lakehouse/inputs/invalid.ndjson", data)
    config = replace(
        source_config,
        source_uri=obj["uri"],
        source_generation=obj["generation"],
        source_sha256=hashlib.sha256(data).hexdigest(),
        run_id="invalid-001",
    )
    manifest = run(config, objects, lambda: SharedSession(spark))
    assert manifest["artifacts"]["clean"]["row_count"] == 0
    assert manifest["artifacts"]["quarantine"]["row_count"] == 1
    client = FakeBQ()
    publish(manifest, objects, client)
    assert not any(j.endswith("_clean") for j in client.submitted)
    assert len(client.submitted) == 4
