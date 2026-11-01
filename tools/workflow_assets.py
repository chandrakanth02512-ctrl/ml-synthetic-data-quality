"""Offline deterministic packaging/rendering; separately guarded future GCS staging."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import yaml

from synthetic_quality_lakehouse.config import canonical
from synthetic_quality_vertex.config import fingerprint, read_ref
from synthetic_quality_workflows.plan import ROOT, ObjectRef, compile_plan, validate

REPO = Path(__file__).resolve().parents[1]


def package(destination):
    with ZipFile(destination, "x") as archive:
        for name in (
            "synthetic_quality",
            "synthetic_quality_lakehouse",
            "synthetic_quality_vertex",
            "synthetic_quality_workflows",
        ):
            for path in sorted((REPO / "src" / name).rglob("*")):
                if not path.is_file() or path.suffix not in {".py", ".sql"}:
                    continue
                entry = ZipInfo(path.relative_to(REPO / "src").as_posix(), (2026, 1, 1, 0, 0, 0))
                entry.compress_type = ZIP_DEFLATED
                entry.external_attr = 0o100644 << 16
                archive.writestr(entry, path.read_bytes())
    return hashlib.sha256(Path(destination).read_bytes()).hexdigest()


def render(bundle, destination):
    compiled = compile_plan(bundle["plan"], bundle["reference"])
    definition = yaml.safe_load((REPO / "workflows/pipeline.yaml").read_text())
    definition["main"]["steps"][0]["initialize"]["assign"][0]["p"] = compiled
    text = yaml.safe_dump(definition, sort_keys=False, width=1000)
    if len(text.encode()) > 128 * 1024:
        raise ValueError("Workflow source exceeds 128 KiB bound")
    with Path(destination).open("x") as stream:
        stream.write(text)
    return fingerprint(compiled)


def stage(config, store):
    # Stage only a clean reviewed checkout; an enabled plan requires separate pilot approval.
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO, check=True, capture_output=True, text=True
    ).stdout.strip()
    dirty = subprocess.run(
        [
            "git",
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            "src",
            "workflows",
            "tools/workflow_assets.py",
        ],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if dirty or config["revision"] != revision:
        raise ValueError("Use the exact clean reviewed feature revision")
    validate(config)
    # Required inputs are read and hash-verified before any staging writes.
    for name in ("training", "evaluation", "truth"):
        read_ref(store, ObjectRef(**config[name]))
    with TemporaryDirectory(prefix="workflow-package-") as directory:
        path = Path(directory) / "pipeline.zip"
        sha = package(path)
        config = {
            **config,
            "package": store.create(ROOT + "/code/" + sha + ".zip", path.read_bytes()),
        }
    bootstrap = (REPO / "src/synthetic_quality_workflows/bootstrap.py").read_bytes()
    sha = hashlib.sha256(bootstrap).hexdigest()
    config["bootstrap"] = store.create(ROOT + "/code/" + sha + ".py", bootstrap)
    validate(config)
    reference = store.create(ROOT + "/plans/" + fingerprint(config) + ".json", canonical(config))
    return {"plan": config, "reference": reference}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("package", "render", "stage"))
    parser.add_argument("path", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--approve-staging", action="store_true")
    args = parser.parse_args()
    if args.action == "package":
        print(package(args.path))
    elif args.action == "render":
        if not args.output:
            parser.error("A new output path is required")
        print(render(json.loads(args.path.read_text()), args.output))
    else:
        if not args.approve_staging:
            parser.error("Separate explicit staging approval required")
        from google.cloud import storage

        from synthetic_quality_lakehouse.storage import GCSObjects
        from synthetic_quality_workflows.plan import PROJECT

        print(
            json.dumps(
                stage(
                    json.loads(args.path.read_text()), GCSObjects(storage.Client(project=PROJECT))
                ),
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
