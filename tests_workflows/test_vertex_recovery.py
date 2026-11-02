"""Actual failed Vertex response/reservation and workflow serialization regressions."""

import copy
import json
from pathlib import Path

import pytest
import yaml

from synthetic_quality_lakehouse.config import canonical
from tests_workflows.effects import Effects
from tests_workflows.engine import Engine, WorkflowError
from tools.workflow_assets import render


@pytest.fixture
def captured_vertex():
    return json.loads(Path("tests_workflows/fixtures/captured_vertex_failure.json").read_text())


def no_effects(*args):
    raise AssertionError("No cloud effects permitted")


@pytest.mark.parametrize("project", ["gcp-intelligent-data-quality", "984592102391"])
def test_vertex_project_aliases_preserve_exact_request_validation(
    definition, captured_vertex, project
):
    # Isolate identity from the independently corrupted command captured in this job.
    stage = captured_vertex["stage"]
    job = captured_vertex["job"]
    job["jobSpec"]["workerPoolSpecs"][0]["containerSpec"]["command"] = copy.deepcopy(
        stage["spec"]["jobSpec"]["workerPoolSpecs"][0]["containerSpec"]["command"]
    )
    job["name"] = f"projects/{project}/locations/asia-south1/customJobs/8554311789649592320"
    assert Engine(definition, no_effects).run("validate_job", job=job, stage=stage) is True


@pytest.mark.parametrize(
    "name",
    [
        None,
        8554311789649592320,
        "projects/unrelated/locations/asia-south1/customJobs/123",
        "projects/984592102392/locations/asia-south1/customJobs/123",
        "projects/0984592102391/locations/asia-south1/customJobs/123",
        "projects/984592102391/locations/us-central1/customJobs/123",
        "projects/984592102391/locations/asia-south1/customJobs/",
        "projects/984592102391/locations/asia-south1/customJobs/abc",
        "projects/984592102391/locations/asia-south1/customJobs/123/other",
        "projects/984592102391/locations/asia-south1/customJobs/123\n",
        "projects/984592102391/locations/asia-south1/customJobs/123?other=1",
        "https://asia-south1-aiplatform.googleapis.com/v1/projects/984592102391/locations/asia-south1/customJobs/123",
    ],
)
def test_vertex_rejects_unrelated_or_ambiguous_names(definition, captured_vertex, name):
    stage = captured_vertex["stage"]
    job = copy.deepcopy(stage["spec"])
    if name is not None:
        job["name"] = name
    with pytest.raises(WorkflowError, match="WORKLOAD_IDENTITY_CONFLICT"):
        Engine(definition, no_effects).run("validate_job", job=job, stage=stage)


def test_original_vertex_identity_error_is_reproduced(definition, captured_vertex):
    old = (
        '${not text.match_regex(job.name, "^projects/gcp-intelligent-data-quality/'
        'locations/asia-south1/customJobs/[0-9]+$")}'
    )
    assert Engine(definition, no_effects).value(old, {"job": captured_vertex["job"]}) is True


def test_captured_bootstrap_reproduces_syntax_error_and_is_not_trusted(definition, captured_vertex):
    job = captured_vertex["job"]
    program = job["jobSpec"]["workerPoolSpecs"][0]["containerSpec"]["command"][-1]
    with pytest.raises(SyntaxError, match="unexpected character after line continuation") as error:
        compile(program, "captured-vertex-bootstrap", "exec")
    assert error.value.lineno == 30
    assert "\\ )" in program.splitlines()[29]
    with pytest.raises(WorkflowError, match="WORKLOAD_REQUEST_CONFLICT"):
        Engine(definition, no_effects).run("validate_job", job=job, stage=captured_vertex["stage"])


def test_rendered_bootstrap_is_literal_exact_and_compiles(bundle, tmp_path):
    target = tmp_path / "fixed.yaml"
    render(bundle, target)
    text = target.read_text()
    expected = Path("src/synthetic_quality_workflows/bootstrap.py").read_text()
    parsed = yaml.safe_load(text)
    compiled = parsed["main"]["steps"][0]["initialize"]["assign"][0]["p"]
    command = compiled["stages"][2]["spec"]["jobSpec"]["workerPoolSpecs"][0]["containerSpec"][
        "command"
    ]
    # Test the YAML representation too: a PyYAML round trip alone missed the failure.
    nodes = []

    def walk(node):
        if isinstance(node, yaml.ScalarNode):
            if node.value == expected:
                nodes.append(node)
        elif isinstance(node, yaml.SequenceNode):
            for child in node.value:
                walk(child)
        elif isinstance(node, yaml.MappingNode):
            for key, child in node.value:
                walk(key)
                walk(child)

    walk(yaml.compose(text))
    assert len(nodes) == 1 and nodes[0].style == "|"
    assert command[:2] == ["python", "-c"] and command[-1] == expected
    assert "\\\n" not in text
    compile(command[-1], "rendered-vertex-bootstrap", "exec")
    compile(json.loads(json.dumps(command))[-1], "json-vertex-bootstrap", "exec")
    # The previous serializer introduced the physical continuation sequences.
    old = yaml.safe_dump(parsed, sort_keys=False, width=1000)
    assert "\\\n" in old


def recovery_effects(store, captured_vertex, stage):
    reservation = captured_vertex["reservation"]
    uri = captured_vertex["reservation_reference"]["uri"]
    store.create(uri, canonical(reservation))
    p = {
        "bucket": uri[5:].split("/", 1)[0],
        "prefix": "lakehouse/orchestration/runs/workflow-smoke-001",
        "plan_hash": reservation["plan_hash"],
        "stages": [stage],
        "poll_seconds": 10,
        "poll_limit": 90,
    }
    effects = Effects(store, p)
    effects.jobs[captured_vertex["job"]["name"]] = captured_vertex["job"]
    return p, effects


def test_corrected_request_cannot_resume_existing_corrupted_reservation(
    definition, store, captured_vertex
):
    stage = captured_vertex["stage"]
    p, effects = recovery_effects(store, captured_vertex, stage)
    before = copy.deepcopy(store.client.bucket(p["bucket"]).objects)
    with pytest.raises(WorkflowError, match="CHECKPOINT_CONFLICT"):
        Engine(definition, effects).run(
            "workload", p=p, stage=stage, execution_id="no-real-execution", mode="resume"
        )
    assert effects.submissions == []
    assert store.client.bucket(p["bucket"]).objects == before


def test_matching_old_reservation_adopts_failed_job_but_never_retries(
    definition, store, captured_vertex
):
    stage = copy.deepcopy(captured_vertex["stage"])
    stage["spec"] = captured_vertex["reservation"]["spec"]
    p, effects = recovery_effects(store, captured_vertex, stage)
    engine = Engine(definition, effects)
    job = engine.run("workload", p=p, stage=stage, execution_id="no-real-execution", mode="resume")
    assert job["state"] == "JOB_STATE_FAILED"
    with pytest.raises(WorkflowError, match="WORKLOAD_FAILED"):
        engine.run("wait_for_job", p=p, stage=stage, job=job)
    assert effects.submissions == []
