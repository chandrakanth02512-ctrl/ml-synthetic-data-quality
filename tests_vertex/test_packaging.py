import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest

from synthetic_quality_lakehouse.config import canonical
from synthetic_quality_vertex import bootstrap, train
from tests_vertex.conftest import reference

ROOT = Path(__file__).resolve().parents[1]


def assets():
    sys.path.insert(0, str(ROOT / "tools"))
    spec = importlib.util.spec_from_file_location("vertex_assets", ROOT / "tools/vertex_assets.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_deterministic_importable_code_package_preserves_sources(tmp_path):
    tool = assets()
    one, two = tmp_path / "one.zip", tmp_path / "two.zip"
    assert tool.package(one) == tool.package(two)
    with pytest.raises(FileExistsError):
        tool.package(one)
    with ZipFile(one) as archive:
        names = archive.namelist()
        assert "synthetic_quality_vertex/train.py" in names
        assert "synthetic_quality/model.py" in names
        assert "synthetic_quality_lakehouse/sql/publish.sql" in names
        assert all(n.endswith((".py", ".sql")) for n in names)
        assert (
            archive.read("synthetic_quality/model.py")
            == (ROOT / "src/synthetic_quality/model.py").read_bytes()
        )
    # The configuration uses sklearn; import from the installed test environment plus ZIP.
    script = (
        "import sys; sys.path.insert(0,sys.argv[1]); "
        "from synthetic_quality_vertex.config import code_digest; "
        "assert len(code_digest())==64"
    )
    subprocess.run(
        [sys.executable, "-c", script, str(one)], cwd=tmp_path, capture_output=True, check=True
    )


def test_source_fixtures_keep_labels_outside_transaction_inputs(tmp_path):
    target = tmp_path / "fixtures"
    assets().fixtures(target)
    for name, count in (("training", 256), ("evaluation", 100)):
        rows = [json.loads(line) for line in (target / (name + ".ndjson")).read_text().splitlines()]
        assert len(rows) == count and all("is_anomaly" not in r for r in rows)
    truth = [json.loads(line) for line in (target / "truth.ndjson").read_text().splitlines()]
    assert sum(r["is_anomaly"] for r in truth) == 5
    with pytest.raises(FileExistsError):
        assets().fixtures(target)


def test_staging_is_immutable_and_plan_binds_config(config, store):
    tool = assets()
    plan = tool.stage_bundle(config, "a" * 40, store)
    assert tool.stage_bundle(config, "a" * 40, store) == plan
    from synthetic_quality_vertex.custom_job import JobPlan

    assert JobPlan.from_dict(plan).training == config


def test_bootstrap_runs_real_training_only_after_code_config_verified(
    config, store, monkeypatch, capsys
):
    code = reference(
        store, "gs://offline-data/lakehouse/vertex/code.zip", b"fixture-verified-package"
    )
    cfg = reference(
        store, "gs://offline-data/lakehouse/vertex/config.json", canonical(config.to_dict())
    )
    argv = ["bootstrap", *[str(v) for r in (code, cfg) for v in (r.uri, r.generation, r.sha256)]]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr("google.cloud.storage.Client", lambda **kwargs: store.client)

    def run_module(name, **kwargs):
        assert name == "synthetic_quality_vertex.train"
        train.main()

    monkeypatch.setattr("runpy.run_module", run_module)
    bootstrap.main()
    assert json.loads(capsys.readouterr().out)["status"] == "COMPLETE"


def test_bootstrap_bad_bytes_or_namespace_never_import_training(config, store, monkeypatch):
    record = reference(store, "gs://offline-data/lakehouse/vertex/code.zip", b"code")
    with pytest.raises(ValueError):
        bootstrap.download(store.client, record.uri, record.generation, "0" * 64, 100)
    with pytest.raises(ValueError):
        bootstrap.download(store.client, record.uri, record.generation, record.sha256, 1)
    with pytest.raises(ValueError):
        bootstrap.download(store.client, "gs://gcp-idq-data-984592102391/x", 1, "0" * 64, 100)
    monkeypatch.setattr(sys, "argv", ["bootstrap"])
    with pytest.raises(ValueError):
        bootstrap.main()


def test_cli_staging_requires_approval_before_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["assets", "stage-bundle", str(tmp_path / "config")])
    with pytest.raises(SystemExit):
        assets().main()


def test_staging_bundle_cli_uses_reviewed_config_without_recreating_identity(
    config, store, tmp_path, monkeypatch, capsys
):
    tool = assets()
    path = tmp_path / "config.json"
    path.write_bytes(canonical(config.to_dict()))
    monkeypatch.setattr(sys, "argv", ["assets", "stage-bundle", str(path), "--approve-staging"])
    monkeypatch.setattr("google.cloud.storage.Client", lambda **kwargs: store.client)
    outputs = iter(("a" * 40, ""))
    from types import SimpleNamespace

    monkeypatch.setattr(
        tool.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=next(outputs))
    )
    tool.main()
    plan = json.loads(capsys.readouterr().out)
    assert plan["training"] == config.to_dict()


def test_truth_staging_cli_creates_only_evaluation_object(
    config, store, tmp_path, monkeypatch, capsys
):
    tool = assets()
    path = tmp_path / "truth.ndjson"
    path.write_bytes(store.read(config.truth.uri)[0])
    monkeypatch.setattr(sys, "argv", ["assets", "stage-truth", str(path), "--approve-staging"])
    monkeypatch.setattr("google.cloud.storage.Client", lambda **kwargs: store.client)
    tool.main()
    assert json.loads(capsys.readouterr().out)["uri"].endswith("vertex-smoke-001.ndjson")


def test_pr4_files_and_existing_application_preserved():
    import hashlib

    baseline = json.loads((ROOT / "vertex/base-baseline.json").read_text())
    for item in baseline["files"]:
        data = (ROOT / item["path"]).read_bytes()
        assert (
            hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
            == item["git_blob_sha"]
        )


def test_new_ci_is_offline_and_manual_job_is_not_a_workflow_trigger():
    text = (ROOT / ".github/workflows/vertex-checks.yml").read_text()
    assert "id-token:" not in text and "google-github-actions/auth" not in text
    assert "gcloud " not in text and "secrets." not in text
