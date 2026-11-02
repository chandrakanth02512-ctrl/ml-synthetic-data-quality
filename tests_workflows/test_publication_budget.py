"""Reproduce run-002 script budget exhaustion without submitting BigQuery jobs."""

import json
import re
from pathlib import Path

import pytest
from google.api_core.exceptions import BadRequest

from synthetic_quality_lakehouse import bigquery as loader
from synthetic_quality_lakehouse.config import canonical
from synthetic_quality_vertex import predictions
from synthetic_quality_vertex import train as training
from synthetic_quality_vertex.config import code_digest
from synthetic_quality_workflows import runner
from tests_workflows.conftest import manifests_for_plan
from tests_workflows.test_runner import bq_client


@pytest.fixture
def prepared(plan, store, data_pair):
    return manifests_for_plan(plan, store, data_pair)[0]


def billing_client():
    api = bq_client()
    original_query = api.query
    api.publication_caps = []
    captured = json.loads(
        Path("tests_workflows/fixtures/captured_publication_billing_failure.json").read_text()
    )

    def query(sql, *, job_config, job_id, **kwargs):
        job = original_query(sql, job_config=job_config, job_id=job_id, **kwargs)
        remaining = job_config.maximum_bytes_billed
        api.publication_caps.append(remaining)
        # Replay captured child-job charges followed by the failed 20-MiB INSERT.
        minimum = 10 * 1024 * 1024
        observed = captured["observed_successful_child_bytes_billed"]
        assert sum(observed) == captured["bytes_billed_before_failure"] == 9 * minimum
        costs = [*observed, captured["next_statement_required_bytes"]]
        # Conservative tiny-table test model for the remaining literal table references.
        # This verifies headroom, not a prediction of BigQuery's optimizer or billing.
        mentions = len(re.findall(r"`[^`]+`", sql))
        costs += [minimum] * max(0, mentions - 11)
        for cost in costs:
            if remaining < cost:
                job.error = BadRequest(
                    f"Query exceeded limit for bytes billed: {remaining}. "
                    f"{cost} or higher required. at [58:3]"
                )
                break
            remaining -= cost
        return job

    api.query = query
    return api


def test_captured_cap_fails_before_publication_and_is_not_retried(prepared, store, monkeypatch):
    captured = json.loads(
        Path("tests_workflows/fixtures/captured_publication_billing_failure.json").read_text()
    )
    monkeypatch.setattr(loader, "PUBLICATION_MAX_BYTES_BILLED", captured["maximum_bytes_billed"])
    api = billing_client()
    expected = "5628160. 20971520 or higher required"
    with pytest.raises(BadRequest, match=expected):
        runner.run_vertex(prepared, store, api)
    submitted = list(api.submitted)
    with pytest.raises(BadRequest, match=expected):
        runner.run_vertex(prepared, store, api)
    assert api.submitted == submitted
    # Changing a failed job's budget must not silently reuse or replace its fixed identity.
    monkeypatch.setattr(loader, "PUBLICATION_MAX_BYTES_BILLED", 512 * 1024 * 1024)
    with pytest.raises(ValueError, match="different configuration"):
        runner.run_vertex(prepared, store, api)
    assert api.submitted == submitted


def test_bounded_script_budget_covers_all_three_publications_and_replays(prepared, store):
    api = billing_client()
    report = runner.run_vertex(prepared, store, api)
    assert report["status"] == "COMPLETE" and report["reconciled"] is True
    assert api.publication_caps == [512 * 1024 * 1024] * 3
    assert loader.PUBLICATION_MAX_BYTES_BILLED == predictions.PUBLICATION_MAX_BYTES_BILLED
    submitted = list(api.submitted)
    assert runner.run_vertex(prepared, store, api) == report
    assert api.submitted == submitted


def test_captured_model_cannot_be_silently_adopted_under_changed_code(store, monkeypatch):
    completion = json.loads(
        Path("tests_workflows/fixtures/captured_vertex_model_completion.json").read_text()
    )
    config = training.validate_completion(completion)
    assert completion["request"]["code_sha256"] != code_digest()
    request_uri = config.root + "/request.json"
    original = canonical(completion["request"])
    store.create(request_uri, original)

    def forbidden(*args):
        raise AssertionError("Existing run must never silently retrain")

    monkeypatch.setattr(training, "train_model", forbidden)
    with pytest.raises(ValueError, match="Immutable run/object conflict"):
        training.run(config, store)
    assert store.read(request_uri)[0] == original
