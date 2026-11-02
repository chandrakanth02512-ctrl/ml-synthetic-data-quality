"""Regressions from the first pilot's captured Dataproc response; no cloud calls."""

import copy
import json
from pathlib import Path

import pytest

from tests_workflows.engine import Engine, WorkflowError


@pytest.fixture
def captured():
    return json.loads(Path("tests_workflows/fixtures/captured_dataproc_training.json").read_text())


@pytest.fixture
def engine(definition):
    def forbidden(*args):
        raise AssertionError("Comparator tests must have no cloud effects")

    return Engine(definition, forbidden)


@pytest.mark.parametrize("value", ["standard", "false", 0, None, True])
def test_missing_properties_return_false_without_type_error(engine, value):
    assert engine.run("includes", actual={}, expected={"property": value}) is False


@pytest.mark.parametrize("actual,expected", [("false", False), (False, "false"), (0, False)])
def test_mixed_types_are_never_coerced(engine, actual, expected):
    assert engine.run("includes", actual=actual, expected=expected) is False


def test_boolean_false_default_is_preserved(engine):
    assert engine.run("includes", actual={}, expected={"flag": False}) is True
    assert engine.run("includes", actual=False, expected=False) is True


def test_captured_response_matches_without_mutating_request_or_response(engine, captured):
    original = copy.deepcopy(captured)
    assert engine.run("validate_job", job=captured["response"], stage=captured["stage"])
    assert captured == original


@pytest.mark.parametrize("properties", [None, "missing-map", []])
def test_missing_or_malformed_dataproc_properties_reject_cleanly(engine, captured, properties):
    runtime = captured["response"]["runtimeConfig"]
    if properties is None:
        del runtime["properties"]
    else:
        runtime["properties"] = properties
    with pytest.raises(WorkflowError, match="WORKLOAD_REQUEST_CONFLICT"):
        engine.run("validate_job", job=captured["response"], stage=captured["stage"])


@pytest.mark.parametrize("change", ["value", "boolean", "alias", "identity", "account", "args"])
def test_captured_response_rejects_drift(engine, captured, change):
    job = captured["response"]
    properties = job["runtimeConfig"]["properties"]
    if change == "value":
        properties["spark:spark.executor.cores"] = "8"
    elif change == "boolean":
        properties["spark:spark.dynamicAllocation.enabled"] = False
    elif change == "alias":
        properties["spark.executor.cores"] = "8"
    elif change == "identity":
        job["name"] += "-other"
    elif change == "account":
        job["environmentConfig"]["executionConfig"]["serviceAccount"] = "other@example.com"
    else:
        job["pysparkBatch"]["args"].append("unapproved")
    with pytest.raises(WorkflowError, match="WORKLOAD_(REQUEST|IDENTITY)_CONFLICT"):
        engine.run("validate_job", job=job, stage=captured["stage"])


def test_only_recognized_prefixes_are_normalized_and_values_preserved(engine):
    properties = {
        "spark:spark.flag": False,
        "dataproc:dataproc.tier": "standard",
        "spark:spark.string": "false",
        "other:spark.flag": "unchanged",
        "spark:other.flag": "unchanged",
    }
    assert engine.run("normalize_dataproc_properties", properties=properties) == {
        "spark.flag": False,
        "dataproc.tier": "standard",
        "spark.string": "false",
        "other:spark.flag": "unchanged",
        "spark:other.flag": "unchanged",
    }


@pytest.mark.parametrize("first,second", [(False, "false"), (False, 0), ("4", "8")])
def test_conflicting_aliases_rejected_in_both_orders(engine, first, second):
    for properties in (
        {"spark.flag": first, "spark:spark.flag": second},
        {"spark:spark.flag": second, "spark.flag": first},
    ):
        with pytest.raises(WorkflowError, match="WORKLOAD_REQUEST_CONFLICT"):
            engine.run("normalize_dataproc_properties", properties=properties)


def test_equivalent_aliases_are_accepted(engine):
    assert engine.run(
        "normalize_dataproc_properties",
        properties={"spark.flag": False, "spark:spark.flag": False},
    ) == {"spark.flag": False}


def test_workflows_comparison_types_and_short_circuit(engine):
    for expression in ('${"standard" == false}', "${false == 0}"):
        with pytest.raises(WorkflowError, match="incompatible operand types") as failure:
            engine.value(expression, {})
        assert failure.value.value["tags"] == ["TypeError"]
    assert engine.value('${false and "standard" == false}', {}) is False
    assert engine.value('${null == "standard"}', {}) is False
    assert engine.value("${1 == 1.0}", {}) is True


def test_interpreter_rejects_inline_map_expression(engine):
    with pytest.raises(AssertionError, match="Unsupported workflow expression: Dict"):
        engine.value(
            '${map.merge(job, {"properties": normalized_properties})}',
            {"job": {}, "normalized_properties": {}},
        )


def test_original_missing_property_expression_reproduces_pilot_error(engine, captured):
    properties = captured["stage"]["spec"]["runtimeConfig"]["properties"]
    response = captured["response"]["runtimeConfig"]["properties"]
    assert properties["dataproc.tier"] == "standard"
    with pytest.raises(WorkflowError, match="str and bool"):
        engine.value(
            "${not (key in actual) and expected[key] == false}",
            {"key": "dataproc.tier", "actual": response, "expected": properties},
        )
