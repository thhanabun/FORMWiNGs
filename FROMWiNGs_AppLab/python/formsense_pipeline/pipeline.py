"""Sliding-window running-form feature extraction and local CSV recording."""

import csv
import math
import statistics
from collections import deque
from pathlib import Path

from .filters import Calibration, SensorFilter
from .protocol import FEATURE_KEYS, RAW_COLUMNS, ImuSample

G_M_S2 = 9.80665

FILTERED_COLUMNS = [
    "seq",
    "timestamp_s",
    "acc_x_filtered_g",
    "acc_y_filtered_g",
    "acc_z_filtered_g",
    "vertical_dynamic_g",
    "gyro_x_cal_dps",
    "gyro_y_cal_dps",
    "gyro_z_cal_dps",
    "roll_deg",
    "pitch_deg",
]

FEATURE_COLUMNS = ["window_id", "start_time_s", "end_time_s", "sample_count", *FEATURE_KEYS, "form_label"]


def _mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def _peaks(rows: list[dict[str, float]], threshold: float, minimum_gap_s: float = 0.25) -> list[int]:
    peak_indices: list[int] = []
    for index in range(1, len(rows) - 1):
        value = rows[index]["vertical_dynamic_g"]
        if value < threshold or value < rows[index - 1]["vertical_dynamic_g"] or value <= rows[index + 1]["vertical_dynamic_g"]:
            continue
        if peak_indices and rows[index]["timestamp_s"] - rows[peak_indices[-1]]["timestamp_s"] < minimum_gap_s:
            if value > rows[peak_indices[-1]]["vertical_dynamic_g"]:
                peak_indices[-1] = index
            continue
        peak_indices.append(index)
    return peak_indices


def _vertical_oscillation_cm(rows: list[dict[str, float]]) -> float:
    if len(rows) < 3:
        return 0.0
    velocity = [0.0]
    for previous, current in zip(rows, rows[1:]):
        dt = current["timestamp_s"] - previous["timestamp_s"]
        acceleration = (previous["vertical_dynamic_g"] + current["vertical_dynamic_g"]) * 0.5 * G_M_S2
        velocity.append(velocity[-1] + acceleration * dt)
    duration = rows[-1]["timestamp_s"] - rows[0]["timestamp_s"]
    if duration <= 0:
        return 0.0
    drift_rate = velocity[-1] / duration
    corrected_velocity = [
        value - drift_rate * (rows[index]["timestamp_s"] - rows[0]["timestamp_s"])
        for index, value in enumerate(velocity)
    ]
    displacement = [0.0]
    for index in range(1, len(rows)):
        dt = rows[index]["timestamp_s"] - rows[index - 1]["timestamp_s"]
        displacement.append(displacement[-1] + (corrected_velocity[index - 1] + corrected_velocity[index]) * 0.5 * dt)
    return _clamp((max(displacement) - min(displacement)) * 100.0, 0.0, 40.0)


def extract_features(rows: list[dict[str, float]]) -> dict[str, float]:
    if len(rows) < 3:
        return {key: 0.0 for key in FEATURE_KEYS}
    dynamic = [row["vertical_dynamic_g"] for row in rows]
    baseline = _mean(dynamic)
    deviation = statistics.pstdev(dynamic) if len(dynamic) > 1 else 0.0
    impact_threshold = max(0.035, baseline + 0.65 * deviation)
    impacts = _peaks(rows, impact_threshold)
    step_intervals = [
        rows[current]["timestamp_s"] - rows[previous]["timestamp_s"]
        for previous, current in zip(impacts, impacts[1:])
    ]
    cadence = (60.0 / _mean(step_intervals)) if step_intervals else 0.0

    rise_rates = []
    for previous, current in zip(rows, rows[1:]):
        dt = current["timestamp_s"] - previous["timestamp_s"]
        if dt > 0:
            rise_rates.append(max(0.0, current["vertical_dynamic_g"] - previous["vertical_dynamic_g"]) / dt)
    loading_rate = max(rise_rates, default=0.0)

    gct_values: list[float] = []
    flight_values: list[float] = []
    stance_threshold = max(0.015, deviation * 0.28)
    for offset, impact_index in enumerate(impacts):
        left = impact_index
        right = impact_index
        while left > 0 and abs(rows[left - 1]["vertical_dynamic_g"]) > stance_threshold:
            left -= 1
        while right < len(rows) - 1 and abs(rows[right + 1]["vertical_dynamic_g"]) > stance_threshold:
            right += 1
        gct_s = max(0.0, rows[right]["timestamp_s"] - rows[left]["timestamp_s"])
        gct_values.append(gct_s)
        if offset < len(impacts) - 1:
            step_s = rows[impacts[offset + 1]]["timestamp_s"] - rows[impact_index]["timestamp_s"]
            flight_values.append(max(0.0, step_s - gct_s))
    timing_balance_ms = (_mean(gct_values) - _mean(flight_values)) * 1000.0 if flight_values else 0.0

    impact_amplitudes = [rows[index]["vertical_dynamic_g"] for index in impacts]
    left = impact_amplitudes[::2]
    right = impact_amplitudes[1::2]
    pair_reference = (_mean(left) + _mean(right)) * 0.5
    asymmetry = abs(_mean(left) - _mean(right)) / pair_reference * 100.0 if right and pair_reference > 1e-6 else 0.0

    sharpness = _clamp((loading_rate - 1.0) / 7.0, 0.0, 1.0)
    peak_strength = _clamp((_mean(impact_amplitudes) - 0.03) / 0.17, 0.0, 1.0)
    heel_likelihood = _clamp(0.65 * sharpness + 0.35 * peak_strength, 0.0, 1.0)

    return {
        "cadence_spm": round(_clamp(cadence, 0.0, 260.0), 3),
        "vertical_oscillation_cm": round(_vertical_oscillation_cm(rows), 3),
        "gct_flight_balance_ms": round(_clamp(timing_balance_ms, -1000.0, 1000.0), 3),
        "impact_loading_rate_bw_s": round(_clamp(loading_rate, 0.0, 100.0), 3),
        "trunk_forward_lean_deg": round(_mean([row["pitch_deg"] for row in rows]), 3),
        "left_right_asymmetry_pct": round(_clamp(asymmetry, 0.0, 100.0), 3),
        "heel_strike_likelihood": round(heel_likelihood, 3),
    }


class CsvRecorder:
    def __init__(self, output_dir: Path, session_id: str):
        output_dir.mkdir(parents=True, exist_ok=True)
        self.raw_path = output_dir / f"{session_id}_raw.csv"
        self.filtered_path = output_dir / f"{session_id}_filtered.csv"
        self.feature_path = output_dir / f"{session_id}_features.csv"
        self._files: dict[Path, object] = {}
        self._writers: dict[Path, csv.DictWriter] = {}
        self._open(self.raw_path, RAW_COLUMNS)
        self._open(self.filtered_path, FILTERED_COLUMNS)
        self._open(self.feature_path, FEATURE_COLUMNS)

    def _open(self, path: Path, columns: list[str]) -> None:
        is_new = not path.exists() or path.stat().st_size == 0
        handle = path.open("a", newline="", encoding="utf-8", buffering=1)
        writer = csv.DictWriter(handle, fieldnames=columns)
        if is_new:
            writer.writeheader()
        self._files[path] = handle
        self._writers[path] = writer

    def _append(self, path: Path, columns: list[str], row: dict[str, object]) -> None:
        self._writers[path].writerow({key: row.get(key, "") for key in columns})

    def raw(self, sample: ImuSample) -> None:
        self._append(self.raw_path, RAW_COLUMNS, sample.__dict__)

    def filtered(self, seq: int, row: dict[str, float]) -> None:
        self._append(self.filtered_path, FILTERED_COLUMNS, {"seq": seq, **row})

    def features(self, row: dict[str, object]) -> None:
        self._append(self.feature_path, FEATURE_COLUMNS, row)

    def close(self) -> None:
        for handle in self._files.values():
            handle.close()
        self._files.clear()
        self._writers.clear()


class RunningFormPipeline:
    def __init__(
        self,
        output_dir: Path,
        session_id: str,
        form_label: str = "UNLABELED",
        calibration: Calibration | None = None,
        window_s: float = 5.0,
        stride_s: float = 1.0,
    ):
        self.window_s = window_s
        self.stride_s = stride_s
        self.form_label = form_label
        self.filter = SensorFilter(calibration or Calibration())
        self.recorder = CsvRecorder(output_dir, session_id)
        self.window: deque[dict[str, float]] = deque()
        self.window_id = 0
        self.next_emit_time: float | None = None

    def ingest(self, sample: ImuSample) -> dict[str, float] | None:
        self.recorder.raw(sample)
        filtered = self.filter.process(sample)
        self.recorder.filtered(sample.seq, filtered)
        self.window.append(filtered)
        cutoff = sample.timestamp_s - self.window_s
        while self.window and self.window[0]["timestamp_s"] < cutoff:
            self.window.popleft()
        if self.next_emit_time is None:
            self.next_emit_time = sample.timestamp_s + self.window_s
        if sample.timestamp_s < self.next_emit_time or len(self.window) < 3:
            return None
        rows = list(self.window)
        if rows[-1]["timestamp_s"] - rows[0]["timestamp_s"] < self.window_s * 0.85:
            return None
        features = extract_features(rows)
        self.window_id += 1
        feature_row: dict[str, object] = {
            "window_id": self.window_id,
            "start_time_s": round(rows[0]["timestamp_s"], 4),
            "end_time_s": round(rows[-1]["timestamp_s"], 4),
            "sample_count": len(rows),
            **features,
            "form_label": self.form_label,
        }
        self.recorder.features(feature_row)
        self.next_emit_time += self.stride_s
        return features

    def close(self) -> None:
        self.recorder.close()
