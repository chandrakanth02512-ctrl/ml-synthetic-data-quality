import copy
from pathlib import Path

import pytest
import yaml

from synthetic_quality_lakehouse.config import canonical
from tests_workflows.effects import Effects, Interrupted
from tests_workflows.engine import Engine, WorkflowError


def run(definition, effects, mode="start", execution="execution-001"):
    engine = Engine(definition, effects, execution)
    p = effects.p
    return engine.run("main", args={"confirm": "RUN " + p["run_id"], "mode": mode}), engine


@pytest.fixture
def effects(definition, store):
    return Effects(store, definition["main"]["steps"][0]["initialize"]["assign"][0]["p"])


def test_actual_yaml_success_and_replay_without_new_workloads(definition, effects):
    result, engine = run(definition, effects)
    assert result["status"] == "COMPLETE"
    assert effects.submissions == ["training", "evaluation", "vertex"]
    assert [log["json"]["event"] for log in engine.logs] == [
        "START",
        "STAGE_START",
        "STAGE_START",
        "STAGE_START",
        "COMPLETE",
    ]
    result, _ = run(definition, effects, "resume", "execution-002")
    assert result["status"] == "COMPLETE" and len(effects.submissions) == 3


def test_resume_original_comparison_failure_adopts_prefixed_training_job(definition, effects):
    class PrefixedResponse:
        def __call__(self, method, args):
            response = copy.deepcopy(effects(method, args))
            job = response.get("body", {})
            if isinstance(job, dict) and "pysparkBatch" in job:
                properties = job["runtimeConfig"]["properties"]
                job["runtimeConfig"]["properties"] = {
                    ("spark:" if key.startswith("spark.") else "dataproc:") + key: value
                    for key, value in properties.items()
                }
            return response

    # Recreate the pre-fix control plane, with the same request and object identities.
    broken = copy.deepcopy(definition)
    broken["validate_job"]["steps"] = broken["validate_job"]["steps"][2:]
    broken["validate_job"]["steps"][0]["spec_matches"]["args"]["actual"] = "${job}"
    missing = broken["includes"]["steps"][0]["maps"]["switch"][0]["steps"][1]
    missing["entries"]["for"]["steps"][0]["default_false"]["switch"][0]["condition"] = (
        "${not (key in actual) and expected[key] == false}"
    )
    arguments = {"confirm": "RUN " + effects.p["run_id"], "mode": "start"}
    with pytest.raises(WorkflowError, match="CONTROL_PLANE_ERROR"):
        Engine(broken, PrefixedResponse()).run("main", args=arguments)
    assert effects.submissions == ["training"]
    bucket = effects.store.client.bucket(effects.p["bucket"])
    evidence = copy.deepcopy(bucket.objects)
    arguments["mode"] = "resume"
    result = Engine(definition, PrefixedResponse(), "execution-002").run("main", args=arguments)
    assert result["status"] == "COMPLETE"
    assert effects.submissions == ["training", "evaluation", "vertex"]
    assert all(bucket.objects[path] == original for path, original in evidence.items())


def test_template_disabled_and_manual_confirmation_before_any_effect(definition, effects):
    def forbidden(*args):
        raise AssertionError("No HTTP effect allowed")

    raw = yaml.safe_load(Path("workflows/pipeline.yaml").read_text())
    with pytest.raises(WorkflowError, match="DISABLED"):
        Engine(raw, forbidden).run("main", args={})
    for args in (
        {},
        {"confirm": "wrong", "mode": "start"},
        {"confirm": "RUN " + effects.p["run_id"], "mode": "auto"},
    ):
        with pytest.raises(WorkflowError):
            Engine(definition, forbidden).run("main", args=args)


def test_duplicate_start_stops_without_submitting_again(definition, effects):
    run(definition, effects)
    with pytest.raises(WorkflowError, match="DUPLICATE_REQUEST"):
        run(definition, effects, execution="execution-002")
    assert len(effects.submissions) == 3


@pytest.mark.parametrize("stage", ["training", "evaluation", "vertex"])
def test_ambiguous_create_reconciles_on_explicit_resume_without_duplicate(
    definition, effects, stage
):
    effects.lost_ack = stage
    with pytest.raises(WorkflowError, match="AMBIGUOUS_SUBMISSION"):
        run(definition, effects)
    before = list(effects.submissions)
    result, _ = run(definition, effects, "resume", "execution-002")
    assert result["status"] == "COMPLETE"
    assert effects.submissions.count(stage) == before.count(stage) == 1
    assert len(effects.submissions) == 3


@pytest.mark.parametrize("stage", ["training", "evaluation", "vertex"])
def test_interruption_after_reservation_never_submits_unproven_work(definition, effects, stage):
    effects.interrupt_at = stage + "/attempt.json"
    with pytest.raises(Interrupted):
        run(definition, effects)
    before = list(effects.submissions)
    with pytest.raises(WorkflowError, match="AMBIGUOUS_SUBMISSION"):
        run(definition, effects, "resume", "execution-002")
    assert effects.submissions == before


def test_interruption_after_resource_receipt_recovers(definition, effects):
    effects.interrupt_at = "training/receipt.json"
    with pytest.raises(Interrupted):
        run(definition, effects)
    result, _ = run(definition, effects, "resume", "execution-002")
    assert result["status"] == "COMPLETE"
    assert effects.submissions == ["training", "evaluation", "vertex"]


@pytest.mark.parametrize(
    "stage,state",
    [
        ("training", "FAILED"),
        ("evaluation", "CANCELLED"),
        ("vertex", "JOB_STATE_FAILED"),
        ("vertex", "JOB_STATE_CANCELLED"),
        ("vertex", "JOB_STATE_PARTIALLY_SUCCEEDED"),
    ],
)
def test_failed_workloads_stop_and_preserve_records(definition, effects, stage, state):
    effects.states[stage] = state
    with pytest.raises(WorkflowError, match="WORKLOAD_FAILED") as error:
        run(definition, effects)
    assert error.value.value["stage"] == stage
    before = list(effects.submissions)
    with pytest.raises(WorkflowError, match="WORKLOAD_FAILED"):
        run(definition, effects, "resume", "execution-002")
    assert effects.submissions == before


@pytest.mark.parametrize("stage,state", [("training", "RUNNING"), ("vertex", "JOB_STATE_RUNNING")])
def test_poll_timeout_is_bounded_and_does_not_cancel_or_resubmit(definition, effects, stage, state):
    effects.states[stage] = state
    with pytest.raises(WorkflowError, match="POLL_TIMEOUT"):
        run(definition, effects)
    effects.states.pop(stage)
    assert run(definition, effects, "resume", "execution-002")[0]["status"] == "COMPLETE"
    assert len(effects.submissions) == 3


def test_safe_get_retries_and_permission_denied_does_not_look_missing(definition, effects):
    url = effects.p["objects"][0]["url"]
    effects.errors[url] = [503, 429]
    assert run(definition, effects)[0]["status"] == "COMPLETE"
    effects.errors[url] = [403]
    with pytest.raises(WorkflowError) as error:
        run(definition, effects, "resume", "execution-002")
    assert error.value.value["http_code"] == 403
    assert len(effects.submissions) == 3


def test_input_hash_bootstrap_validation_blocks_submission(definition, effects):
    p = effects.p
    p["bootstrap_hash"] = "wrong"
    with pytest.raises(WorkflowError, match="BOOTSTRAP_HASH_MISMATCH"):
        run(definition, effects)
    assert not effects.submissions


@pytest.mark.parametrize("fault", ["reconciled", "artifacts_verified", "publication", "plan_hash"])
def test_final_artifact_and_reconciliation_gate(definition, effects, fault):
    run(definition, effects)
    url = effects.p["stages"][-1]["result_url"]

    def change(report):
        if fault == "publication":
            report["publications"]["predictions"]["status"] = "FAILED"
        else:
            report[fault] = False if fault != "plan_hash" else "different"

    effects.corrupt(url, change)
    with pytest.raises(
        WorkflowError, match="FINAL_RECONCILIATION_FAILED|BIGQUERY_PUBLICATION_FAILED"
    ):
        run(definition, effects, "resume", "execution-002")
    assert len(effects.submissions) == 3


def test_vertex_inventory_pagination_and_duplicate_detection(definition, effects):
    effects.page_vertex = True
    assert run(definition, effects)[0]["status"] == "COMPLETE"
    effects.duplicate_vertex = True
    with pytest.raises(WorkflowError, match="AMBIGUOUS_VERTEX_INVENTORY"):
        run(definition, effects, "resume", "execution-002")
    assert len(effects.submissions) == 3


@pytest.mark.parametrize("fault", ["image", "accelerator", "environment", "identity", "retry"])
def test_existing_job_drift_is_not_trusted_by_label(definition, effects, fault):
    run(definition, effects)
    job = next(j for j in effects.jobs.values() if "/customJobs/" in j["name"])
    worker = job["jobSpec"]["workerPoolSpecs"][0]
    if fault == "image":
        worker["containerSpec"]["imageUri"] = "untrusted:latest"
    elif fault == "accelerator":
        worker["machineSpec"]["acceleratorCount"] = 1
    elif fault == "environment":
        worker["containerSpec"]["env"].append({"name": "EXTRA", "value": "unsafe"})
    elif fault == "identity":
        job["jobSpec"]["serviceAccount"] = "other"
    else:
        job["jobSpec"]["scheduling"]["disableRetries"] = False
    with pytest.raises(WorkflowError, match="WORKLOAD_REQUEST_CONFLICT|UNEXPECTED_ACCELERATOR"):
        run(definition, effects, "resume", "execution-002")
    assert len(effects.submissions) == 3


def test_checkpoint_conflict_and_lost_ack_never_grant_ownership(definition, effects):
    effects.lost_ack = "training/attempt.json"
    with pytest.raises(WorkflowError, match="CHECKPOINT_AMBIGUOUS"):
        run(definition, effects)
    assert not effects.submissions
    bucket, path = effects.marker("training")
    data, generation = effects.store.client.bucket(bucket).objects[path]
    import json

    old = json.loads(data)
    old["plan_hash"] = "different"
    effects.store.client.bucket(bucket).objects[path] = canonical(old), generation
    with pytest.raises(WorkflowError, match="CHECKPOINT_CONFLICT"):
        run(definition, effects, "resume", "execution-002")
    assert not effects.submissions


def test_recursive_comparison_handles_empty_and_proto_false_defaults(definition, effects):
    engine = Engine(definition, effects)
    assert engine.run("includes", actual={"x": []}, expected={"x": [], "flag": False})
    assert not engine.run("includes", actual={"x": [1]}, expected={"x": []})
    assert not engine.run("includes", actual={"x": 1}, expected={"x": [1]})
    assert not engine.run("includes", actual=[], expected={})


def test_failed_failure_checkpoint_does_not_mask_original_error(definition, effects):
    effects.states["training"] = "FAILED"
    effects.lost_ack = "failure.json"
    with pytest.raises(WorkflowError, match="WORKLOAD_FAILED"):
        run(definition, effects)


def test_offline_engine_rejects_unsupported_syntax(definition, effects):
    corrupted = copy.deepcopy(definition)
    corrupted["main"]["steps"][0]["initialize"]["assign"].append({"bad": "${p.run_id[0:1]}"})
    with pytest.raises(AssertionError, match="slicing"):
        Engine(corrupted, effects).run("main", args={})


def test_concurrent_resume_cannot_submit_between_another_execution_reservation_and_post(
    definition, effects
):
    # A second execution runs inside the first one's post-reservation boundary.
    original = effects.__class__.__call__
    intercepted = [False]

    def interleave(method, args):
        response = original(effects, method, args)
        if (
            method == "http.post"
            and "upload/storage" in args["url"]
            and args["query"]["name"].endswith("training/attempt.json")
            and not intercepted[0]
        ):
            intercepted[0] = True
            with pytest.raises(WorkflowError, match="AMBIGUOUS_SUBMISSION"):
                Engine(definition, effects, "concurrent-execution").run(
                    "main",
                    args={"confirm": "RUN " + effects.p["run_id"], "mode": "resume"},
                )
        return response

    result = Engine(definition, interleave).run(
        "main",
        args={"confirm": "RUN " + effects.p["run_id"], "mode": "start"},
    )
    assert result["status"] == "COMPLETE" and intercepted[0]
    assert effects.submissions == ["training", "evaluation", "vertex"]


def test_replaced_output_generation_is_rejected(definition, effects):
    run(definition, effects)
    artifact = effects.store.create(
        "gs://" + effects.p["bucket"] + "/lakehouse/orchestration/model.joblib",
        b"model",
    )
    effects.corrupt(
        effects.p["stages"][-1]["result_url"],
        lambda report: report.update(verified_objects=[artifact] * 17),
    )
    bucket = effects.store.client.bucket(effects.p["bucket"])
    path = "lakehouse/orchestration/model.joblib"
    bucket.objects[path] = b"replaced", artifact["generation"] + 1
    with pytest.raises(WorkflowError, match="ARTIFACT_GENERATION_CHANGED"):
        run(definition, effects, "resume", "execution-002")


def test_unexpected_dataproc_payload_is_not_reused(definition, effects):
    run(definition, effects)
    effects.jobs[effects.p["stages"][0]["name"]]["pysparkBatch"]["pythonFileUris"] = [
        "gs://other/code.zip"
    ]
    with pytest.raises(WorkflowError, match="WORKLOAD_REQUEST_CONFLICT"):
        run(definition, effects, "resume", "execution-002")
    assert len(effects.submissions) == 3


@pytest.mark.parametrize("code", [400, 403, 429])
def test_rejected_submission_is_classified_and_not_automatically_retried(definition, effects, code):
    effects.errors[effects.p["stages"][0]["collection"]] = [code]
    with pytest.raises(WorkflowError, match="WORKLOAD_SUBMISSION_REJECTED") as error:
        run(definition, effects)
    assert error.value.value["http_code"] == code
    assert not effects.submissions
    with pytest.raises(WorkflowError, match="AMBIGUOUS_SUBMISSION"):
        run(definition, effects, "resume", "execution-002")
    assert not effects.submissions
