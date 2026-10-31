import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest

from synthetic_quality_lakehouse.schemas import schema_json

ROOT = Path(__file__).resolve().parents[1]


def tool(name):
    sys.path.insert(0, str(ROOT / "tools"))
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_package_is_deterministic_importable_and_has_no_private_files(tmp_path):
    package = tool("package_lakehouse").package
    first, second = tmp_path / "first.zip", tmp_path / "second.zip"
    assert package(first) == package(second)
    with pytest.raises(FileExistsError):
        package(first)
    with ZipFile(first) as archive:
        names = archive.namelist()
        assert "synthetic_quality/schema.py" in names
        assert "synthetic_quality_lakehouse/sql/publish.sql" in names
        assert all(n.endswith((".py", ".sql")) for n in names)
        assert all("credential" not in n and "receipt" not in n for n in names)
    script = (
        "import sys; sys.path.insert(0,sys.argv[1]); "
        "from synthetic_quality_lakehouse.config import code_digest; "
        "assert len(code_digest())==64"
    )
    subprocess.run(
        [sys.executable, "-S", "-c", script, str(first)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )


def test_fixture_reuses_original_generator_and_refuses_overwrite(tmp_path):
    generate = tool("generate_lakehouse_source").generate
    path = tmp_path / "input.ndjson"
    result = generate(path, rows=256, invalid=True)
    assert result["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert (result["rows"], result["expected_clean"], result["expected_quarantined"]) == (
        256,
        252,
        4,
    )
    with pytest.raises(FileExistsError):
        generate(path)


def test_exported_schemas_and_managed_batch_config_match_code():
    for kind in ("clean", "quarantine", "quality", "ledger", "runs"):
        assert json.loads(
            (ROOT / "lakehouse/schemas" / (kind + ".json")).read_text()
        ) == schema_json(kind)
    body = json.loads((ROOT / "lakehouse/batch.example.json").read_text())
    assert body["runtimeConfig"]["version"] == "3.0"
    props = body["runtimeConfig"]["properties"]
    assert props["spark.executor.instances"] == "2"
    assert props["spark.dynamicAllocation.enabled"] == "false"
    config = body["environmentConfig"]["executionConfig"]
    assert config["authenticationConfig"]["userWorkloadAuthenticationType"] == "SERVICE_ACCOUNT"
    assert config["ttl"] == "600s"


def test_all_original_files_remain_byte_identical():
    baseline = json.loads((ROOT / "lakehouse/application-baseline.json").read_text())
    assert len(baseline["files"]) == 46
    for entry in baseline["files"]:
        data = (ROOT / entry["path"]).read_bytes()
        assert (
            hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
            == entry["git_blob_sha"]
        )


def test_feature_workflow_cannot_authenticate_deploy_or_execute():
    text = (ROOT / ".github/workflows/lakehouse-checks.yml").read_text()
    assert "id-token:" not in text and "google-github-actions/auth" not in text
    assert "gcloud " not in text and "secrets." not in text
    assert "pytest -c tests_lakehouse/pytest.ini" in text


def test_staging_replay_is_create_only_and_input_conflict_stops(tmp_path, objects):
    fixture = tmp_path / "source.ndjson"
    tool("generate_lakehouse_source").generate(fixture, rows=4)
    module = tool("stage_lakehouse")
    first = module.stage(fixture, "a" * 40, "pilot-001", objects.client)
    assert module.stage(fixture, "a" * 40, "pilot-001", objects.client) == first
    fixture.write_bytes(b"{}\n")
    with pytest.raises(ValueError, match="conflict"):
        module.stage(fixture, "a" * 40, "pilot-001", objects.client)


def test_staging_cli_refuses_without_approval_before_authentication(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sys, "argv", ["stage", "--input", str(tmp_path / "x"), "--revision", "a" * 40]
    )
    with pytest.raises(SystemExit):
        tool("stage_lakehouse").main()
