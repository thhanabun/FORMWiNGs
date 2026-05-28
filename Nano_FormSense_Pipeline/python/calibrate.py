#!/usr/bin/env python3
"""Create waist-mounted gyro bias and neutral torso pitch calibration."""

import argparse
import csv
import math
import statistics
from pathlib import Path

from formsense_pipeline.filters import Calibration


def read_values(path: Path) -> list[dict[str, float]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return [{key: float(value) for key, value in row.items() if key != "seq"} for row in csv.DictReader(handle)]


def build_calibration(stationary_rows: list[dict[str, float]]) -> Calibration:
    if not stationary_rows:
        raise ValueError("stationary recording has no samples")
    gyro_bias = tuple(statistics.fmean(row[f"gyro_{axis}_dps"] for row in stationary_rows) for axis in "xyz")
    ax = statistics.fmean(row["acc_x_g"] for row in stationary_rows)
    ay = statistics.fmean(row["acc_y_g"] for row in stationary_rows)
    az = statistics.fmean(row["acc_z_g"] for row in stationary_rows)
    neutral_pitch = math.degrees(math.atan2(-ax, math.sqrt(ay * ay + az * az)))
    return Calibration(gyro_bias_dps=tuple(gyro_bias), neutral_pitch_deg=neutral_pitch)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--stationary",
        required=True,
        type=Path,
        help="Raw CSV recorded while standing upright with the waist device mounted and still.",
    )
    parser.add_argument("--out", default=Path("calibration.json"), type=Path)
    args = parser.parse_args()
    calibration = build_calibration(read_values(args.stationary))
    calibration.save(args.out)
    print(f"saved calibration to {args.out}")
    print(f"gyro_bias_dps={calibration.gyro_bias_dps}")
    print(f"neutral_pitch_deg={calibration.neutral_pitch_deg:.3f}")


if __name__ == "__main__":
    main()
