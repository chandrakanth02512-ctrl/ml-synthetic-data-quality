"""Create a deterministic code-only Dataproc ZIP locally; never uploads/submits."""

import argparse
import hashlib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


def package(destination):
    root = Path(__file__).resolve().parents[1] / "src"
    paths = [root / "synthetic_quality" / n for n in ("__init__.py", "schema.py")]
    paths += [
        p
        for p in (root / "synthetic_quality_lakehouse").rglob("*")
        if p.is_file() and p.suffix in {".py", ".sql"}
    ]
    with ZipFile(destination, "x", compression=ZIP_DEFLATED) as archive:
        for path in sorted(paths):
            entry = ZipInfo(path.relative_to(root).as_posix(), (2026, 1, 1, 0, 0, 0))
            entry.compress_type = ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, path.read_bytes())
    return hashlib.sha256(Path(destination).read_bytes()).hexdigest()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(package(args.destination))
