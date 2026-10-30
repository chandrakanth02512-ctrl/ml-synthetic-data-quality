from dataclasses import replace

import pytest

from synthetic_quality.cloud_config import CloudConfig, parse_config, validate_object_name


@pytest.fixture
def config():
    return CloudConfig("sample-project", "synthetic-test-bucket", "smoke-001")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project_id", ""),
        ("project_id", "gs://project"),
        ("bucket", ""),
        ("bucket", "gs://bucket"),
        ("bucket", "bad/bucket"),
        ("bucket", "GoogleBucket"),
        ("bucket", "goog-reserved"),
        ("bucket", "my-google-bucket"),
        ("bucket", "127.0.0.1"),
        ("run_id", ""),
        ("run_id", "../escape"),
        ("run_id", "x" * 65),
        ("prefix", ""),
        ("prefix", "/absolute"),
        ("prefix", "a/../b"),
        ("prefix", "a//b"),
        ("prefix", "a/"),
        ("prefix", "x" * 201),
        ("training_rows", 0),
        ("training_rows", True),
        ("training_rows", 1.5),
        ("evaluation_rows", -1),
        ("evaluation_rows", 100001),
        ("training_seed", -1),
        ("evaluation_seed", 2**32),
        ("training_seed", True),
        ("evaluation_seed", 42),
    ],
)
def test_invalid_configuration_fails_before_io(config, field, value):
    with pytest.raises(ValueError):
        replace(config, **{field: value})


@pytest.mark.parametrize(
    "name",
    [
        "",
        "../x",
        "a/../b",
        "/a",
        "a/",
        "a//b",
        "a\\b",
        "a\nb",
        "a%2Fb",
        "gs://bucket/a",
        "x" * 1025,
    ],
)
def test_unsafe_object_names(name):
    with pytest.raises(ValueError):
        validate_object_name(name)


def test_path_layout_and_independent_sizes(config):
    config = replace(config, prefix="portfolio/phase1", training_rows=256, evaluation_rows=100)
    assert config.object_name("raw-data", "training.csv") == (
        "portfolio/phase1/raw-data/smoke-001/training.csv"
    )
    assert config.pipeline_config().training_rows == 256
    assert config.pipeline_config().evaluation_rows == 100


def test_environment_configuration_and_cli_precedence(monkeypatch):
    for key, value in {
        "GCP_PROJECT_ID": "sample-project",
        "GCS_BUCKET": "test-bucket",
        "RUN_ID": "env-run",
        "TRAINING_ROWS": "256",
    }.items():
        monkeypatch.setenv(key, value)
    config = parse_config(["--run-id", "cli-run", "--evaluation-rows", "100"])
    assert config.run_id == "cli-run"
    assert config.training_rows == 256
    assert config.evaluation_rows == 100


@pytest.mark.parametrize("arguments", [[], ["--training-rows", "abc"], ["--training-rows", "0"]])
def test_cli_missing_or_invalid_config_exits_with_usage(monkeypatch, arguments, capsys):
    for key in ("GCP_PROJECT_ID", "GCS_BUCKET", "RUN_ID"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(SystemExit) as error:
        parse_config(arguments)
    assert error.value.code == 2
    assert "error:" in capsys.readouterr().err


def test_invalid_environment_number_is_rejected(monkeypatch):
    monkeypatch.setenv("TRAINING_ROWS", "not-an-integer")
    with pytest.raises(SystemExit) as error:
        parse_config([])
    assert error.value.code == 2
