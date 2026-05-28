#!/usr/bin/env python3
"""App Lab entrypoint for FROMWiNGs.

Runs the UNO Q Linux model pipeline and sends compact prediction JSON back to
the UNO Q MCU, where sketch/sketch.ino publishes it over BLE as FROMWiNGs.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
WORKING_DIR = Path.cwd().resolve()


def _first_existing_directory(candidates: list[Path], marker: str) -> Path:
    for candidate in candidates:
        if (candidate / marker).exists():
            return candidate
    checked = "\n".join(str(candidate / marker) for candidate in candidates)
    raise ModuleNotFoundError(f"Cannot find {marker}. Checked:\n{checked}")


def _first_existing_file(candidates: list[Path]) -> Path:
    for candidate in candidates:
        if candidate.exists():
            return candidate
    checked = "\n".join(str(candidate) for candidate in candidates)
    raise FileNotFoundError(f"Cannot find model file. Checked:\n{checked}")


def _optional_existing_file(candidates: list[Path]) -> Path | None:
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


PYTHON_DIR = _first_existing_directory(
    [
        SCRIPT_DIR,
        SCRIPT_DIR / "python",
        SCRIPT_DIR.parent / "python",
        WORKING_DIR,
        WORKING_DIR / "python",
    ],
    "formsense_pipeline",
)
APP_ROOT = PYTHON_DIR.parent

sys.path.insert(0, str(PYTHON_DIR))

from uno_q_bridge_live_inference import main


if __name__ == "__main__":
    sys.argv = [
        sys.argv[0],
        "--model",
        str(
            _first_existing_file(
                [
                    APP_ROOT / "running_form_xgboost.json",
                    APP_ROOT / "model" / "running_form_xgboost.json",
                    SCRIPT_DIR / "running_form_xgboost.json",
                    SCRIPT_DIR / "model" / "running_form_xgboost.json",
                    SCRIPT_DIR.parent / "running_form_xgboost.json",
                    SCRIPT_DIR.parent / "model" / "running_form_xgboost.json",
                    WORKING_DIR / "running_form_xgboost.json",
                    WORKING_DIR / "model" / "running_form_xgboost.json",
                    APP_ROOT / "running_form_transformer_fp16.tflite",
                    APP_ROOT / "model" / "running_form_transformer_fp16.tflite",
                    SCRIPT_DIR / "running_form_transformer_fp16.tflite",
                    SCRIPT_DIR / "model" / "running_form_transformer_fp16.tflite",
                    SCRIPT_DIR.parent / "running_form_transformer_fp16.tflite",
                    SCRIPT_DIR.parent / "model" / "running_form_transformer_fp16.tflite",
                    WORKING_DIR / "running_form_transformer_fp16.tflite",
                    WORKING_DIR / "model" / "running_form_transformer_fp16.tflite",
                ]
            )
        ),
        *(
            [
                "--normalizer",
                str(normalizer),
            ]
            if (
                normalizer := _optional_existing_file(
                    [
                        APP_ROOT / "running_form_normalizer.json",
                        APP_ROOT / "model" / "running_form_normalizer.json",
                        SCRIPT_DIR / "running_form_normalizer.json",
                        SCRIPT_DIR / "model" / "running_form_normalizer.json",
                        SCRIPT_DIR.parent / "running_form_normalizer.json",
                        SCRIPT_DIR.parent / "model" / "running_form_normalizer.json",
                        WORKING_DIR / "running_form_normalizer.json",
                        WORKING_DIR / "model" / "running_form_normalizer.json",
                    ]
                )
            )
            else []
        ),
        "--enable-metric-alerts",
        "--sample-rate-hz",
        "50",
        "--worker-batch-size",
        "80",
        "--worker-batch-wait-s",
        "0.05",
        "--ble-idle-shutdown-min",
        "30",
        "--output-dir",
        str(Path.home() / "formsense_data" / "live"),
    ]
    main()
