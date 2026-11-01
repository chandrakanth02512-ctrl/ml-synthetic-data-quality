"""Local Vertex code/fixture packaging and separately approved immutable staging."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from package_lakehouse import package as package_lakehouse

from synthetic_quality_lakehouse.config import canonical
from synthetic_quality_vertex.config import ObjectRef, TrainingConfig, decode_rows

ROOT = Path(__file__).resolve().parents[1]


def package(destination):
    package_lakehouse(destination)
    with ZipFile(destination, "a") as archive:
        existing = set(archive.namelist())
        for name in ("synthetic_quality", "synthetic_quality_vertex"):
            for source in sorted((ROOT / "src" / name).glob("*.py")):
                relative = source.relative_to(ROOT / "src").as_posix()
                if relative in existing:
                    continue
                entry = ZipInfo(relative, (2026, 1, 1, 0, 0, 0))
                entry.compress_type = ZIP_DEFLATED
                entry.external_attr = 0o100644 << 16
                archive.writestr(entry, source.read_bytes())
    return hashlib.sha256(Path(destination).read_bytes()).hexdigest()


def fixtures(destination, training_rows=256, evaluation_rows=100):
    from synthetic_quality.data_generator import generate_transactions

    if not 2 <= training_rows <= 10_000 or not 1 <= evaluation_rows <= 10_000:
        raise ValueError("Fixture size outside reviewed bounds")
    destination = Path(destination)
    destination.mkdir()  # Preserve any existing directory/data; never overwrite.
    train = generate_transactions(training_rows, seed=42, dataset_name="training")
    evaluation = generate_transactions(
        evaluation_rows,
        seed=123,
        anomaly_fraction=0.05,
        dataset_name="evaluation",
        start_date="2025-02-01",
    )
    for name, frame in (("training", train.transactions), ("evaluation", evaluation.transactions)):
        rows = frame.to_dict("records")
        for row in rows:
            row["timestamp"] = row["timestamp"].isoformat()
        (destination / (name + ".ndjson")).write_bytes(b"".join(canonical(r) + b"\n" for r in rows))
    truth = evaluation.truth.to_dict("records")
    for row in truth:
        row["is_anomaly"] = int(row["is_anomaly"])
    (destination / "truth.ndjson").write_bytes(b"".join(canonical(r) + b"\n" for r in truth))


def stage_bundle(config, revision, store):
    with TemporaryDirectory(prefix="vertex-code-") as directory:
        target = Path(directory) / "vertex.zip"
        package(target)
        code = store.create(
            config.output_uri + "/code/" + revision + "/vertex.zip", target.read_bytes()
        )
    ref = store.create(
        config.output_uri + "/configs/" + config.run_id + ".json", canonical(config.to_dict())
    )
    return {"training": config.to_dict(), "config": ref, "package": code}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("package", "fixtures", "stage-truth", "stage-bundle"))
    parser.add_argument("path", type=Path)
    parser.add_argument("--approve-staging", action="store_true")
    parser.add_argument("--run-id", default="vertex-smoke-001")
    args = parser.parse_args()
    if args.action == "package":
        print(package(args.path))
        return
    if args.action == "fixtures":
        fixtures(args.path)
        return
    if not args.approve_staging:
        parser.error("Separate explicit GCS staging approval required")
    from google.cloud import storage

    from synthetic_quality_lakehouse.storage import GCSObjects

    store = GCSObjects(storage.Client(project="gcp-intelligent-data-quality"))
    if args.action == "stage-truth":
        data = args.path.read_bytes()
        rows = decode_rows(data)
        if (
            len(data) > 10 * 1024 * 1024
            or not 1 <= len(rows) <= 10_000
            or any(
                type(r.get("is_anomaly")) is not int or r["is_anomaly"] not in (0, 1) for r in rows
            )
        ):
            raise ValueError("Invalid evaluation truth")
        # Validate the run namespace using the same strict configuration contract.
        import re

        if not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", args.run_id):
            raise ValueError("Invalid truth namespace")
        print(
            json.dumps(
                store.create(
                    "gs://synthetic-quality-984592102391-data-as1/lakehouse/vertex/truth/"
                    + args.run_id
                    + ".ndjson",
                    data,
                )
            )
        )
        return
    config = TrainingConfig.from_dict(json.loads(args.path.read_text()))
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    dirty = subprocess.run(
        [
            "git",
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            "src",
            "tools/vertex_assets.py",
            "tools/package_lakehouse.py",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if dirty:
        raise ValueError("Use a clean reviewed source checkout")
    plan = stage_bundle(config, revision, store)
    for field in ("config", "package"):
        ObjectRef(**plan[field])
    print(json.dumps(plan, indent=2))


if __name__ == "__main__":
    main()
