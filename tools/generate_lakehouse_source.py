"""Reuse the original generator for a local synthetic NDJSON fixture; no cloud I/O."""

import argparse
import hashlib
import json
from pathlib import Path

from synthetic_quality.data_generator import generate_transactions


def generate(destination, rows=256, seed=42, invalid=False):
    if not 4 <= rows <= 100_000:
        raise ValueError("Fixture rows must be in [4, 100000]")
    frame = generate_transactions(rows, seed=seed, dataset_name="training").transactions
    records = frame.to_dict("records")
    for row in records:
        row["timestamp"] = row["timestamp"].isoformat()
    if invalid:
        records[1]["customer_id"] = None
        records[2]["transaction_amount"] = -1
        records[3]["transaction_id"] = records[0]["transaction_id"]
    data = ("\n".join(json.dumps(row, allow_nan=False) for row in records) + "\n").encode()
    with Path(destination).open("xb") as stream:
        stream.write(data)
    return {
        "rows": rows,
        "sha256": hashlib.sha256(data).hexdigest(),
        "expected_clean": rows - 4 if invalid else rows,
        "expected_quarantined": 4 if invalid else 0,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--rows", type=int, default=256)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--with-invalid", action="store_true")
    args = parser.parse_args()
    print(json.dumps(generate(args.destination, args.rows, args.seed, args.with_invalid)))
