import ast
import copy
import hashlib
import json
import runpy
import sys
from importlib.resources import files
from pathlib import Path
from unittest.mock import Mock
from zipfile import ZipFile

import pytest
import yaml

from synthetic_quality_lakehouse.config import canonical
from synthetic_quality_workflows import bootstrap
from synthetic_quality_workflows.plan import ROOT, compile_plan, validate
from tools import workflow_assets


def test_deterministic_code_only_importable_package_preserves_components(tmp_path):
    a, b = tmp_path / "a.zip", tmp_path / "b.zip"
    assert workflow_assets.package(a) == workflow_assets.package(b)
    assert a.read_bytes() == b.read_bytes()
    with ZipFile(a) as archive:
        assert all(n.endswith((".py", ".sql")) for n in archive.namelist())
        for name in (
            "synthetic_quality/model.py",
            "synthetic_quality_lakehouse/driver.py",
            "synthetic_quality_vertex/train.py",
        ):
            assert archive.read(name) == (Path("src") / name).read_bytes()
        assert "synthetic_quality_lakehouse/sql/publish.sql" in archive.namelist()
    with pytest.raises(FileExistsError):
        workflow_assets.package(a)


def test_render_is_self_contained_disabled_by_default_and_create_only(bundle, tmp_path):
    plan = copy.deepcopy(bundle)
    plan["plan"]["enabled"] = False
    from synthetic_quality_vertex.config import fingerprint

    plan["reference"]["sha256"] = fingerprint(plan["plan"])
    target = tmp_path / "pipeline.yaml"
    workflow_assets.render(plan, target)
    definition = yaml.safe_load(target.read_text())
    p = definition["main"]["steps"][0]["initialize"]["assign"][0]["p"]
    assert p["enabled"] is False and len(p["stages"]) == 3
    assert p["stages"][0]["query"]["batchId"] != p["stages"][1]["query"]["batchId"]
    assert p["stages"][2]["spec"]["jobSpec"]["scheduling"]["disableRetries"] is True
    assert len(target.read_bytes()) < 128 * 1024
    with pytest.raises(FileExistsError):
        workflow_assets.render(plan, target)


@pytest.mark.parametrize(
    "field,value",
    [
        ("runtime_image", "image:latest"),
        ("run_id", "bad/../"),
        ("poll_limit", 121),
        ("training_rows", 257),
        ("evaluation_rows", True),
        ("max_quarantined_rows", 11),
        ("subnetwork", "other-project/region/subnet"),
        ("revision", "main"),
        ("dataset", "dataset;DROP TABLE"),
        ("enabled", "true"),
        ("format_version", 2),
    ],
)
def test_plan_rejects_unreviewed_or_oversized_pilot(plan, field, value):
    plan[field] = value
    with pytest.raises(ValueError):
        validate(plan)


def test_wrong_plan_binding_and_unknown_settings_rejected(bundle):
    bundle["reference"]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="bind"):
        compile_plan(**bundle)
    bundle["plan"]["arbitrary_job"] = "unsafe"
    with pytest.raises(ValueError, match="Exact"):
        validate(bundle["plan"])


def test_bootstrap_rejects_changed_package_before_execution(store, plan, monkeypatch):
    package = store.create(ROOT + "/code/bootstrap-test.zip", b"pinned bytes")
    plan["package"] = package
    ref = store.create(ROOT + "/plans/bootstrap-test.json", canonical(plan))
    monkeypatch.setattr("google.cloud.storage.Client", lambda: store.client)
    args = [
        "bootstrap",
        *[str(v) for r in (package, ref) for v in (r["uri"], r["generation"], r["sha256"])],
        "vertex",
    ]
    args[3] = "0" * 64
    monkeypatch.setattr(sys, "argv", args)
    invoke = Mock(side_effect=AssertionError("Must not import unverified code"))
    monkeypatch.setattr(runpy, "run_module", invoke)
    with pytest.raises(ValueError, match="hash differs"):
        bootstrap.main()
    invoke.assert_not_called()


def test_bootstrap_success_verifies_refs_and_launches_fixed_runner(
    store, plan, tmp_path, monkeypatch
):
    path = tmp_path / "bundle.zip"
    workflow_assets.package(path)
    plan["package"] = store.create(ROOT + "/code/bootstrap-success.zip", path.read_bytes())
    ref = store.create(ROOT + "/plans/bootstrap-success.json", canonical(plan))
    monkeypatch.setattr("google.cloud.storage.Client", lambda: store.client)
    args = [
        "bootstrap",
        *[str(v) for r in (plan["package"], ref) for v in (r["uri"], r["generation"], r["sha256"])],
        "vertex",
    ]
    monkeypatch.setattr(sys, "argv", args)
    monkeypatch.setattr(sys, "path", list(sys.path))

    def launch(module, run_name):
        assert module == "synthetic_quality_workflows.runner" and run_name == "__main__"
        assert json.loads(Path(sys.argv[2]).read_text()) == plan
        assert sys.argv[-2:] == ["--phase", "vertex"]
        assert Path(sys.path[0]).suffix == ".zip"

    invoke = Mock(side_effect=launch)
    monkeypatch.setattr(runpy, "run_module", invoke)
    bootstrap.main()
    invoke.assert_called_once()


def test_staging_gate_no_cloud_and_preserved_base_code(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["assets", "stage", "missing.json"])
    with pytest.raises(SystemExit):
        workflow_assets.main()
    baseline = json.loads(Path("workflows/base-baseline.json").read_text())
    assert len(baseline) == 102

    # The historical PR #5 baseline stays immutable. The root README is the
    # single intentional editorial revision for the portfolio overview;
    # pin both the old and new digest rather than exempting documentation.
    assert baseline["README.md"] == (
        "ddca6deea98651c931c381b39b8e81bdc8d53ab42a65c47215c8bd15d99bcdf4"
    )
    reviewed_readme_digest = (
        "c6b71e73fecf749414e51c2c3d9a91d1b193d4e35b4a8472f25bd2194e4b1cb0"
    )
    for path, digest in baseline.items():
        actual = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        expected = reviewed_readme_digest if path == "README.md" else digest
        assert actual == expected, path


def walk_steps(steps):
    for step in steps:
        assert len(step) == 1
        name, node = next(iter(step.items()))
        yield name, node
        if "for" in node:
            yield from walk_steps(node["for"]["steps"])
        if "try" in node and "steps" in node["try"]:
            yield from walk_steps(node["try"]["steps"])
        if "except" in node:
            yield from walk_steps(node["except"]["steps"])
        for branch in node.get("switch", []):
            if "steps" in branch:
                yield from walk_steps(branch["steps"])


def test_workflows_documented_subset_names_expressions_and_effect_boundaries(definition):
    # Independent structural lint plus behavioral interpretation of the YAML itself.
    # This does not claim the Google deployment compiler has accepted the definition.
    for name, sub in definition.items():
        names = [label for label, _ in walk_steps(sub["steps"])]
        assert len(names) == len(set(names)), name
        for _, node in walk_steps(sub["steps"]):
            if "retry" in node:
                assert node["try"]["call"] == "http.get"
                assert node["retry"]["max_retries"] == 3
            for operation in (node, node.get("try", {})):
                if operation.get("call", "").startswith("http."):
                    assert operation["call"] in {"http.get", "http.post"}
                    assert operation["args"]["auth"] == {"type": "OAuth2"}

    def expressions(value):
        if isinstance(value, dict):
            for item in value.values():
                yield from expressions(item)
        elif isinstance(value, list):
            for item in value:
                yield from expressions(item)
        elif isinstance(value, str) and value.startswith("${"):
            yield value

    for value in expressions(definition):
        assert len(value) <= 400, value
        tree = ast.parse(value[2:-1].replace("if(", "choose("), mode="eval")
        assert not any(isinstance(n, ast.Slice) for n in ast.walk(tree))
    assert len(files("synthetic_quality_workflows").joinpath("bootstrap.py").read_bytes()) < 65536


def test_staging_verifies_inputs_and_preserves_existing_objects(plan, store, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        workflow_assets.subprocess,
        "run",
        lambda args, **kwargs: SimpleNamespace(
            stdout=plan["revision"] if "rev-parse" in args else ""
        ),
    )
    bundle = workflow_assets.stage(plan, store)
    assert compile_plan(**bundle)["run_id"] == plan["run_id"]
    before = {b: dict(v.objects) for b, v in store.client.buckets.items()}
    assert workflow_assets.stage(plan, store) == bundle
    assert before == {b: dict(v.objects) for b, v in store.client.buckets.items()}
    broken = copy.deepcopy(plan)
    broken["training"]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="hash differs"):
        workflow_assets.stage(broken, store)
    assert before == {b: dict(v.objects) for b, v in store.client.buckets.items()}


def test_dirty_or_wrong_revision_staging_stops_before_writes(plan, store, monkeypatch):
    from types import SimpleNamespace

    before = {b: dict(v.objects) for b, v in store.client.buckets.items()}
    monkeypatch.setattr(
        workflow_assets.subprocess, "run", lambda args, **kwargs: SimpleNamespace(stdout="wrong")
    )
    with pytest.raises(ValueError, match="clean reviewed"):
        workflow_assets.stage(plan, store)
    assert before == {b: dict(v.objects) for b, v in store.client.buckets.items()}


def test_runtime_overlay_preserves_model_pins_and_declares_sdk_compatibility():
    def pins(path):
        return {
            line.split("==")[0]: line.split("==")[1]
            for line in Path(path).read_text().splitlines()
            if "==" in line and not line.startswith("#")
        }

    original = pins("requirements-cloud.txt")
    constraints = pins("workflows/runtime-constraints.txt")
    assert "python-dateutil" not in constraints
    assert {k: constraints[k] for k in original if k != "python-dateutil"} == {
        k: v for k, v in original.items() if k != "python-dateutil"
    }
    assert pins("workflows/requirements-runtime.txt") == {
        "google-cloud-bigquery": "3.38.0",
        "python-dateutil": "2.9.0.post0",
    }
    assert "USER 10001:10001" in Path("workflows/Dockerfile").read_text()


def test_spark_control_imports_do_not_require_sklearn_or_vertex_training(plan):
    import subprocess

    script = """
import importlib.abc, sys, json
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in {'numpy','pandas','sklearn','joblib','pyspark'}:
            raise AssertionError('Unnecessary ML/Spark dependency during control import: '+fullname)
sys.meta_path.insert(0, Block())
from synthetic_quality_workflows.runner import validate
validate(json.loads(sys.argv[1]))
"""
    subprocess.run(
        [sys.executable, "-c", script, json.dumps(plan)], check=True, capture_output=True
    )
