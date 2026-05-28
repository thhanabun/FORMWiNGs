#!/usr/bin/env python3
"""Build a personal good-form baseline for realtime waist-wearable feedback."""

import argparse
import csv
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path

from formsense_pipeline.protocol import FEATURE_KEYS


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("baseline.json"))
    args = parser.parse_args()
    rows: list[dict[str, str]] = []
    sources = sorted(args.sessions_dir.glob("*_features.csv"))
    for path in sources:
        with path.open(newline="", encoding="utf-8") as handle:
            rows.extend(row for row in csv.DictReader(handle) if row.get("form_label") == "GOOD_FORM")
    if not rows:
        raise SystemExit("No GOOD_FORM feature windows found; collect coach-verified baseline sessions first.")
    baseline = {
        "sensor_location": "waist",
        "source_label": "GOOD_FORM",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "window_count": len(rows),
        "features": {
            key: {
                "mean": round(statistics.fmean(float(row[key]) for row in rows), 4),
                "std": round(statistics.pstdev(float(row[key]) for row in rows), 4),
            }
            for key in FEATURE_KEYS
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(baseline, indent=2), encoding="utf-8")
    print(f"wrote baseline from {len(rows)} GOOD_FORM windows to {args.output}")


if __name__ == "__main__":
    main()
