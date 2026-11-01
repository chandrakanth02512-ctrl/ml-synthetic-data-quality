import copy
import json
from dataclasses import replace
from unittest.mock import Mock

import pytest
import requests

from synthetic_quality_lakehouse.config import canonical, gcs_parts
from synthetic_quality_vertex import custom_job as jobs
from synthetic_quality_vertex.config import IMAGE
from tests_vertex.conftest import reference


@pytest.fixture
def plan(config, store):
    return jobs.JobPlan(
        config,
        reference(
            store, "gs://offline-data/lakehouse/vertex/config.json", canonical(config.to_dict())
        ),
        reference(
            store, "gs://offline-data/lakehouse/vertex/package.zip", b"reviewed-code-fixture"
        ),
    )


class API:
    def __init__(self, plan):
        self.calls = []
        self.job = {**plan.spec(), "name": jobs.PARENT + "/123", "state": "JOB_STATE_RUNNING"}
        self.lost_ack = False
        self.empty = False

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "POST" and self.lost_ack:
            self.lost_ack = False
            raise TimeoutError("lost acknowledgement")
        if method == "GET" and "?" in path:
            return {"customJobs": [] if self.empty else [self.job]}
        return copy.deepcopy(self.job)


def test_plan_uses_one_cpu_worker_verified_image_no_endpoint_or_build(plan):
    spec = plan.spec()
    worker = spec["jobSpec"]["workerPoolSpecs"][0]
    assert len(spec["jobSpec"]["workerPoolSpecs"]) == 1
    assert worker["replicaCount"] == "1"
    assert worker["containerSpec"]["imageUri"] == IMAGE
    assert worker["machineSpec"] == {"machineType": "n1-standard-4"}
    assert spec["jobSpec"]["scheduling"] == {
        "timeout": "600s",
        "restartJobOnWorkerRestart": False,
        "disableRetries": True,
    }
    assert "serviceAccount" in spec["jobSpec"]
    assert worker["containerSpec"]["command"][:2] == ["python", "-c"]


def test_submission_is_reserved_once_and_status_is_read_only(plan, store):
    api = API(plan)
    result = jobs.submit(plan, store, api)
    assert result["status"] == "SUBMITTED" and result["training_verified"] is False
    with pytest.raises(ValueError, match="reserved"):
        jobs.submit(plan, store, api)
    assert len([c for c in api.calls if c[0] == "POST"]) == 1
    status = jobs.reconcile(plan, store, api)
    assert status["state"] == "JOB_STATE_RUNNING" and status["training_verified"] is False


def test_lost_submit_ack_and_failed_jobs_never_authorize_resubmission(plan, store):
    api = API(plan)
    api.lost_ack = True
    with pytest.raises(TimeoutError):
        jobs.submit(plan, store, api)
    api.job["state"] = "JOB_STATE_FAILED"
    assert jobs.reconcile(plan, store, api)["state"] == "JOB_STATE_FAILED"
    with pytest.raises(ValueError, match="reserved"):
        jobs.submit(plan, store, api)
    assert len([c for c in api.calls if c[0] == "POST"]) == 1


def test_lost_reservation_ack_prevents_any_customjob_submission(plan, store):
    bucket = store.client.buckets["offline-data"]
    bucket.lose_ack = gcs_parts(plan.training.root + "/submission.attempt.json")[1]
    api = API(plan)
    with pytest.raises(RuntimeError, match="acknowledgement"):
        jobs.submit(plan, store, api)
    assert not api.calls
    with pytest.raises(ValueError, match="reserved"):
        jobs.submit(plan, store, api)


def test_missing_reconciliation_match_does_not_resubmit(plan, store):
    api = API(plan)
    api.lost_ack = True
    with pytest.raises(TimeoutError):
        jobs.submit(plan, store, api)
    api.empty = True
    with pytest.raises(ValueError, match="uniquely"):
        jobs.reconcile(plan, store, api)


def test_proto_default_false_can_be_omitted_but_extra_network_is_rejected(plan):
    job = API(plan).job
    del job["jobSpec"]["scheduling"]["restartJobOnWorkerRestart"]
    assert jobs.validate_job(job, plan) is job
    job["jobSpec"]["network"] = "projects/other/global/networks/unreviewed"
    with pytest.raises(ValueError):
        jobs.validate_job(job, plan)


def test_incomplete_reconciliation_inventory_stops_without_retry(plan, store):
    store.reserve(plan.training.root + "/submission.attempt.json", canonical(plan.spec()))
    api = Mock()
    api.request.return_value = {"customJobs": [], "nextPageToken": "repeated"}
    with pytest.raises(ValueError, match="inventory"):
        jobs.reconcile(plan, store, api)
    assert api.request.call_count == 2


@pytest.mark.parametrize("fault", ["image", "replicas", "gpu", "identity", "name"])
def test_changed_job_is_rejected_even_with_matching_labels(plan, fault):
    job = API(plan).job
    worker = job["jobSpec"]["workerPoolSpecs"][0]
    if fault == "image":
        worker["containerSpec"]["imageUri"] = "unreviewed:latest"
    if fault == "replicas":
        worker["replicaCount"] = "2"
    if fault == "gpu":
        worker["machineSpec"]["acceleratorCount"] = 1
    if fault == "identity":
        job["jobSpec"]["serviceAccount"] = "other@invalid"
    if fault == "name":
        job["name"] = "projects/other/locations/us/customJobs/123"
    with pytest.raises(ValueError):
        jobs.validate_job(job, plan)


def test_mismatched_staged_code_or_config_stops_before_reserving(plan, store):
    api = API(plan)
    bad = replace(plan, package=replace(plan.package, sha256="0" * 64))
    with pytest.raises(ValueError, match="differs"):
        jobs.submit(bad, store, api)
    assert not api.calls
    assert store.optional(plan.training.root + "/submission.attempt.json") is None
    with pytest.raises(ValueError):
        replace(plan, config=replace(plan.config, sha256="0" * 64))


def test_cli_plan_and_submission_approval_guard(plan, tmp_path, monkeypatch, capsys):
    from dataclasses import asdict

    path = tmp_path / "plan.json"
    path.write_text(json.dumps(asdict(plan)))
    monkeypatch.setattr("sys.argv", ["jobs", "plan", "--plan", str(path)])
    jobs.main()
    assert json.loads(capsys.readouterr().out)["jobSpec"] == plan.spec()["jobSpec"]
    monkeypatch.setattr("sys.argv", ["jobs", "submit", "--plan", str(path)])
    with pytest.raises(SystemExit):
        jobs.main()


def test_http_transport_disables_auth_post_retries_and_redirects(monkeypatch):
    session = Mock()
    session.request.return_value.status_code = 200
    session.request.return_value.json.return_value = {"ok": True}
    constructor = Mock(return_value=session)
    monkeypatch.setattr("google.auth.default", lambda **kwargs: (Mock(), None))
    monkeypatch.setattr("google.auth.transport.requests.AuthorizedSession", constructor)
    api = jobs.VertexAPI()
    constructor.assert_called_once()
    assert constructor.call_args.kwargs["max_refresh_attempts"] == 0
    assert api.request("GET", jobs.PARENT) == {"ok": True}
    assert session.request.call_args.kwargs["allow_redirects"] is False
    session.request.side_effect = requests.Timeout("fixture")
    with pytest.raises(RuntimeError, match="ambiguous"):
        api.request("POST", jobs.PARENT, {})
    assert session.request.call_count == 2
    with pytest.raises(ValueError):
        api.request("DELETE", jobs.PARENT)


def test_http_failures_stop_without_retry(monkeypatch):
    session = Mock()
    session.request.return_value.status_code = 403
    monkeypatch.setattr("google.auth.default", lambda **kwargs: (Mock(), None))
    monkeypatch.setattr(
        "google.auth.transport.requests.AuthorizedSession", Mock(return_value=session)
    )
    with pytest.raises(RuntimeError, match="403"):
        jobs.VertexAPI().request("POST", jobs.PARENT, {})
    session.request.assert_called_once()
