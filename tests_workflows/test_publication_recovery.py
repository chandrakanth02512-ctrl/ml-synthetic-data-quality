"""Publication-only integration: real saved model/artifacts, SQL and local transactions."""

import copy
import hashlib
import json

import duckdb
import pytest
import sqlglot
from google.api_core.exceptions import NotFound

from synthetic_quality_lakehouse.config import canonical
from synthetic_quality_vertex import train
from synthetic_quality_workflows import publication_recovery as recovery
from synthetic_quality_workflows import runner
from synthetic_quality_workflows.plan import PROJECT, ROOT, fingerprint
from tests_workflows.conftest import manifests_for_plan
from tests_workflows.test_runner import bq_client
from tools.workflow_assets import package


class Job:
    def __init__(self, name, config, error=None):
        self.job_id, self.config, self.error = name, config, error
        self.state = "DONE"
        self.error_result = {"message": str(error)} if error else None

    def to_api_repr(self):
        return {"configuration": self.config}

    def result(self, retry=None, job_retry=None, timeout=None):
        assert retry is None and job_retry is None and timeout > 0
        if self.error:
            raise self.error
        return self


class SQLClient:
    """SDK boundary executing row copies/reconciliation in local DuckDB transactions."""

    def __init__(self, store, manifest):
        self.store, self.jobs, self.submitted = store, {}, []
        self.tables = bq_client().tables
        self.connection = duckdb.connect()
        self.connection.execute(f"ATTACH ':memory:' AS \"{PROJECT}\"")
        self.connection.execute(f'CREATE SCHEMA "{PROJECT}".pilot')
        self.fail_phase = self.lose_ack = self.corrupt_phase = None
        for name, table in self.tables.items():
            self.make_table(name, table.schema)
        prior = manifest["superseded_job"]
        self.jobs[prior] = Job(
            prior,
            {
                "query": {
                    "queryParameters": [
                        {"name": "run_id", "parameterValue": {"value": "workflow-001-training"}}
                    ]
                }
            },
            RuntimeError("Query exceeded limit for bytes billed"),
        )

    def make_table(self, name, schema):
        types = {
            "STRING": "VARCHAR",
            "INTEGER": "BIGINT",
            "INT64": "BIGINT",
            "FLOAT": "DOUBLE",
            "FLOAT64": "DOUBLE",
            "BOOLEAN": "BOOLEAN",
            "BOOL": "BOOLEAN",
            "TIMESTAMP": "TIMESTAMP",
            "DATE": "DATE",
        }
        columns = ", ".join(
            '"' + f.name + '" ' + types[f.field_type] + ("[]" if f.mode == "REPEATED" else "")
            for f in schema
        )
        self.connection.execute("CREATE TABLE " + self.sql_name(name) + " (" + columns + ")")

    @staticmethod
    def sql_name(name):
        return ".".join('"' + part + '"' for part in name.split("."))

    def get_dataset(self, *args, **kwargs):
        return type("Dataset", (), {"location": "asia-south1"})()

    def get_table(self, name, **kwargs):
        if name not in self.tables:
            raise NotFound("missing")
        table = self.tables[name]
        table._properties["numRows"] = str(len(list(self.list_rows(name))))
        return table

    def create_table(self, table, **kwargs):
        name = str(table.reference)
        self.tables[name] = table
        self.make_table(name, table.schema)
        return self.get_table(name)

    def get_job(self, name, **kwargs):
        if name not in self.jobs:
            raise NotFound("missing")
        return self.jobs[name]

    def load_table_from_uri(self, uri, table_id, *, job_config, job_id, **kwargs):
        assert kwargs["retry"] is None
        rows = [json.loads(line) for line in self.store.read(uri)[0].splitlines()]
        config = job_config.to_api_repr()
        config["load"].update(
            {"sourceUris": [uri], "destinationTable": self.tables[table_id].reference.to_api_repr()}
        )
        assert not list(self.list_rows(table_id)), "WRITE_EMPTY"
        fields = [f.name for f in self.tables[table_id].schema]
        self.connection.executemany(
            "INSERT INTO "
            + self.sql_name(table_id)
            + " VALUES ("
            + ",".join("?" for _ in fields)
            + ")",
            [[r[k] for k in fields] for r in rows],
        )
        self.submitted.append(job_id)
        self.jobs[job_id] = Job(job_id, config)
        return self.jobs[job_id]

    def list_rows(self, table_id, **kwargs):
        return self.connection.execute("SELECT * FROM " + self.sql_name(table_id)).fetchall()

    def query(self, sql, *, job_config, job_id, **kwargs):
        assert kwargs["retry"] is None and kwargs["job_retry"] is None
        config = job_config.to_api_repr()
        config["query"]["query"] = sql
        self.submitted.append(job_id)
        error = None
        if sql != "SELECT @recovery_sha AS recovery_sha":
            parameters = {p.name: p.value for p in job_config.query_parameters}
            phase = job_id.rsplit("_", 1)[-1]
            self.connection.execute("BEGIN TRANSACTION")
            try:
                # Execute the actual INSERT SELECT / receipt statements from the script.
                receipt = "model_runs" if phase == "predictions" else "pipeline_runs"
                present = self.connection.execute(
                    "SELECT count(*) FROM "
                    + self.sql_name(PROJECT + ".pilot." + receipt)
                    + " WHERE run_id=?",
                    [parameters["run_id"]],
                ).fetchone()[0]
                if not present:
                    import re

                    for match in re.finditer(r"INSERT INTO[^;]+;", sql):
                        statement = sqlglot.transpile(
                            match.group(), read="bigquery", write="duckdb"
                        )[0]
                        used = {k: v for k, v in parameters.items() if "$" + k in statement}
                        self.connection.execute(statement, used)
                        if self.fail_phase == phase:
                            raise RuntimeError("injected failure inside transaction")
                if phase != "predictions":
                    import re

                    ledger = next(t for t in re.findall(r"`([^`]+)`", sql) if ".stg_ledger_" in t)
                    from synthetic_quality_lakehouse.bigquery import sql_text

                    if self.corrupt_phase == phase:
                        self.connection.execute(
                            "UPDATE "
                            + self.sql_name(PROJECT + ".pilot.clean_transactions")
                            + " SET source_record_sha256='changed'"
                        )
                    q = sql_text("reconcile.sql").format(dataset=PROJECT + ".pilot", ledger=ledger)
                    q = sqlglot.transpile(q, read="bigquery", write="duckdb")[0]
                    observed = self.connection.execute(
                        q, {"run_id": parameters["run_id"]}
                    ).fetchone()
                    assert observed == (
                        parameters["source_rows"],
                        parameters["source_rows"],
                        0,
                        0,
                        0,
                    )
                self.connection.execute("COMMIT")
            except Exception as failure:
                self.connection.execute("ROLLBACK")
                error = failure
        self.jobs[job_id] = Job(job_id, config, error)
        if self.lose_ack and job_id.endswith("_" + self.lose_ack):
            self.lose_ack = None
            raise TimeoutError("lost acknowledgement after commit")
        return self.jobs[job_id]


@pytest.fixture
def prepared(plan, store, data_pair, tmp_path):
    plan = manifests_for_plan(plan, store, data_pair)[0]
    path = tmp_path / "original.zip"
    sha = package(path)
    plan["package"] = store.create(ROOT + "/code/" + sha + ".zip", path.read_bytes())
    plan_ref = store.create(ROOT + "/plans/" + fingerprint(plan) + ".json", canonical(plan))
    report = runner.run_vertex(plan, store, bq_client())
    manifest = {
        "format_version": 1,
        "attempt_id": "publication-recovery-test-001",
        "project": PROJECT,
        "region": "asia-south1",
        "plan": plan_ref,
        "training": report["dataproc"]["training"],
        "evaluation": report["dataproc"]["evaluation"],
        "model": report["vertex"],
        "superseded_job": report["publications"]["training"]["job_id"],
        "implementation_sha256": recovery.implementation_digest(),
        "maximum_bytes_billed": 512 * 1024 * 1024,
    }
    client = SQLClient(store, manifest)
    yield manifest, store, client
    client.connection.close()


def test_real_artifact_publication_reconciles_and_replays_without_gcs_writes(prepared, monkeypatch):
    manifest, store, client = prepared
    before = copy.deepcopy({b: s.objects for b, s in store.client.buckets.items()})
    monkeypatch.setattr(train, "run", lambda *a: pytest.fail("No retraining"))
    monkeypatch.setattr(runner, "run_spark", lambda *a: pytest.fail("No Spark"))
    monkeypatch.setattr(runner, "run_vertex", lambda *a: pytest.fail("No Vertex worker"))
    result = recovery.recover(manifest, store, client)
    assert result["status"] == "COMPLETE"
    assert result["counts"] == {
        "clean_transactions": 356,
        "quarantined_records": 0,
        "quality_results": 24,
        "pipeline_runs": 2,
        "anomaly_predictions": 100,
        "model_runs": 1,
    }
    submitted = list(client.submitted)
    assert recovery.recover(manifest, store, client) == result
    assert client.submitted == submitted
    assert {b: s.objects for b, s in store.client.buckets.items()} == before


def test_rollback_failed_attempt_is_never_replaced(prepared):
    manifest, store, client = prepared
    client.fail_phase = "training"
    with pytest.raises(RuntimeError, match="injected failure"):
        recovery.recover(manifest, store, client)
    assert len(client.list_rows(PROJECT + ".pilot.clean_transactions")) == 0
    submitted = list(client.submitted)
    with pytest.raises(ValueError, match="Existing recovery job failed"):
        recovery.recover(manifest, store, client)
    assert client.submitted == submitted


def test_lost_ack_after_commit_resumes_same_jobs_without_duplicate_rows(prepared):
    manifest, store, client = prepared
    client.lose_ack = "training"
    with pytest.raises(TimeoutError, match="lost acknowledgement"):
        recovery.recover(manifest, store, client)
    assert len(client.list_rows(PROJECT + ".pilot.clean_transactions")) == 256
    assert recovery.recover(manifest, store, client)["status"] == "COMPLETE"
    assert len(client.submitted) == len(set(client.submitted))


def test_competing_attempt_for_same_source_stops_at_anchor(prepared):
    manifest, store, client = prepared
    recovery.recover(manifest, store, client)
    submitted = list(client.submitted)
    other = {**manifest, "attempt_id": "publication-recovery-test-002"}
    with pytest.raises(ValueError, match="different configuration"):
        recovery.recover(other, store, client)
    assert client.submitted == submitted


def test_lost_anchor_ack_recovers_without_starting_duplicate_jobs(prepared):
    manifest, store, client = prepared
    client.lose_ack = "anchor"
    with pytest.raises(TimeoutError, match="lost acknowledgement"):
        recovery.recover(manifest, store, client)
    assert len(client.submitted) == 1
    assert recovery.recover(manifest, store, client)["status"] == "COMPLETE"
    assert len(client.submitted) == len(set(client.submitted))


def test_actual_reconciliation_detects_corruption_and_rolls_back(prepared):
    manifest, store, client = prepared
    client.corrupt_phase = "training"
    with pytest.raises(AssertionError):
        recovery.recover(manifest, store, client)
    assert not client.list_rows(PROJECT + ".pilot.clean_transactions")
    assert not client.list_rows(PROJECT + ".pilot.pipeline_runs")


def test_active_original_job_blocks_recovery_before_any_mutation(prepared):
    manifest, store, client = prepared
    client.jobs[manifest["superseded_job"]].state = "RUNNING"
    with pytest.raises(ValueError, match="Original failed job is not terminal"):
        recovery.recover(manifest, store, client)
    assert not client.submitted


def test_read_only_store_has_no_mutation_interface(prepared):
    _, store, _ = prepared
    read_only = recovery.ReadOnlyObjects(store)
    for method in ("create", "reserve", "delete", "upload"):
        assert not hasattr(read_only, method)


@pytest.mark.parametrize("approve", [False, True])
def test_cli_requires_approval_and_exact_confirmation_before_credentials(
    prepared, tmp_path, monkeypatch, approve
):
    manifest, _, _ = prepared
    raw = canonical(manifest)
    path = tmp_path / "manifest.json"
    path.write_bytes(raw)
    args = [
        "publication-recovery",
        str(path),
        "--sha256",
        hashlib.sha256(raw).hexdigest(),
        "--confirm",
        "WRONG" if approve else "PUBLISH " + manifest["attempt_id"],
        "--output",
        str(tmp_path / "result.json"),
        "--account",
        "operator@example.invalid",
    ]
    if approve:
        args.append("--approve-publication")
    monkeypatch.setattr("sys.argv", args)
    monkeypatch.setattr(
        recovery.subprocess, "run", lambda *a, **k: pytest.fail("No credential calls")
    )
    with pytest.raises(SystemExit):
        recovery.main()


def test_cli_requires_explicit_account_before_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        [
            "publication-recovery",
            str(tmp_path / "manifest.json"),
            "--sha256",
            "0" * 64,
            "--confirm",
            "PUBLISH reviewed-attempt",
            "--output",
            str(tmp_path / "result.json"),
            "--approve-publication",
        ],
    )
    monkeypatch.setattr(
        recovery.subprocess, "run", lambda *a, **k: pytest.fail("No credential calls")
    )
    with pytest.raises(SystemExit):
        recovery.main()


@pytest.mark.parametrize("fault", ["generation", "hash", "budget", "code"])
def test_changed_recovery_inputs_stop_before_any_job(prepared, fault):
    manifest, store, client = prepared
    if fault == "generation":
        manifest["model"]["generation"] += 1
    elif fault == "hash":
        manifest["training"]["sha256"] = "0" * 64
    elif fault == "budget":
        manifest["maximum_bytes_billed"] = 100_000_000
    else:
        manifest["implementation_sha256"] = "0" * 64
    with pytest.raises((ValueError, FileNotFoundError)):
        recovery.recover(manifest, store, client)
    assert not client.submitted


def test_recovery_read_retries_only_transient_download_and_keeps_generation(monkeypatch):
    from unittest.mock import Mock

    from requests.exceptions import ConnectionError

    blob = Mock(size=3, generation=42)
    blob.download_as_bytes.side_effect = [ConnectionError("read timed out"), b"abc"]
    client = Mock()
    client.bucket.return_value.get_blob.return_value = blob
    monkeypatch.setattr(recovery.time, "sleep", lambda seconds: None)
    store = recovery.RecoveryObjects(client)
    assert (
        store.verify(
            {
                "uri": "gs://data/object",
                "generation": 42,
                "sha256": hashlib.sha256(b"abc").hexdigest(),
            }
        )
        == b"abc"
    )
    assert blob.download_as_bytes.call_count == 2
    for call in blob.download_as_bytes.call_args_list:
        assert call.kwargs == {"if_generation_match": 42, "retry": None, "timeout": (10, 30)}


def test_recovery_read_exhaustion_names_object_and_never_writes(monkeypatch):
    from unittest.mock import Mock

    from requests.exceptions import ReadTimeout

    client = Mock()
    client.bucket.return_value.get_blob.side_effect = ReadTimeout("timeout")
    monkeypatch.setattr(recovery.time, "sleep", lambda seconds: None)
    with pytest.raises(RuntimeError, match=r"metadata.*gs://data/object.*attempt=2/2"):
        recovery.RecoveryObjects(client).read("gs://data/object", generation=42)
    assert client.bucket.return_value.get_blob.call_count == 2
    client.bucket.return_value.blob.assert_not_called()


@pytest.mark.parametrize("fault", ["hash", "generation", "permission"])
def test_recovery_read_never_retries_integrity_or_permission_failures(monkeypatch, fault):
    from unittest.mock import Mock

    from google.api_core.exceptions import Forbidden

    blob = Mock(size=3, generation=42)
    blob.download_as_bytes.return_value = b"abc"
    client = Mock()
    client.bucket.return_value.get_blob.return_value = blob
    if fault == "permission":
        client.bucket.return_value.get_blob.side_effect = Forbidden("denied")
    monkeypatch.setattr(recovery.time, "sleep", lambda seconds: pytest.fail("No retry"))
    ref = {
        "uri": "gs://data/object",
        "generation": 43 if fault == "generation" else 42,
        "sha256": "0" * 64 if fault == "hash" else hashlib.sha256(b"abc").hexdigest(),
    }
    with pytest.raises((ValueError, Forbidden)):
        recovery.RecoveryObjects(client).verify(ref)
    assert client.bucket.return_value.get_blob.call_count == 1
