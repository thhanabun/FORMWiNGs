#!/usr/bin/env python3
"""Replay Nano raw IMU CSV through the local/UNO Q-compatible pipeline."""

import argparse
import csv
from pathlib import Path

from formsense_pipeline.filters import Calibration
from formsense_pipeline.pipeline import RunningFormPipeline
from formsense_pipeline.protocol import RAW_COLUMNS, ImuSample


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("data/replay"))
    parser.add_argument("--session-id", default="replay")
    parser.add_argument("--label", default="UNLABELED", choices=["GOOD_FORM", "BAD_FORM", "UNLABELED"])
    parser.add_argument("--calibration", type=Path)
    args = parser.parse_args()
    pipeline = RunningFormPipeline(
        args.output_dir,
        args.session_id,
        args.label,
        Calibration.load(args.calibration),
    )
    emitted = 0
    with args.input.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            sample = ImuSample(
                **{key: int(row[key]) if key == "seq" else float(row[key]) for key in RAW_COLUMNS}
            )
            if pipeline.ingest(sample):
                emitted += 1
    pipeline.close()
    print(f"wrote {emitted} windows to {pipeline.recorder.feature_path}")


if __name__ == "__main__":
    main()
