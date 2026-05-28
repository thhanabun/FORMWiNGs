#!/usr/bin/env python3
"""Combine labeled session feature CSV files into a model-ready training table."""

import argparse
import csv
from pathlib import Path

from formsense_pipeline.pipeline import FEATURE_COLUMNS


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions-dir", type=Path, default=Path("data/sessions"))
    parser.add_argument("--output", type=Path, default=Path("data/training_features.csv"))
    args = parser.parse_args()
    sources = sorted(args.sessions_dir.glob("*_features.csv"))
    if not sources:
        raise SystemExit(f"no *_features.csv files found in {args.sessions_dir}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    row_count = 0
    labels: dict[str, int] = {}
    columns = ["session_id", *FEATURE_COLUMNS]
    with args.output.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=columns)
        writer.writeheader()
        for source in sources:
            session_id = source.name.removesuffix("_features.csv")
            with source.open(newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    label = row.get("form_label", "UNLABELED")
                    if label == "UNLABELED":
                        continue
                    writer.writerow({"session_id": session_id, **row})
                    row_count += 1
                    labels[label] = labels.get(label, 0) + 1
    print(f"wrote {row_count} labeled feature windows from {len(sources)} sessions to {args.output}")
    print(f"label counts: {labels}")


if __name__ == "__main__":
    main()
