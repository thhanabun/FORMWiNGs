"""UNO Q feature extraction and Transformer inference for waist IMU streams."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
from scipy.signal import butter, find_peaks, savgol_filter, sosfiltfilt

from .metric_triggers import MetricTriggerEngine
from .protocol import FEATURE_KEYS, ImuSample

G_TO_MPS2 = 9.80665

FEATURE_DISPLAY_NAMES = {
    "cadence_spm": "Cadence",
    "vertical_oscillation_cm": "Vertical Oscillation",
    "gct_flight_balance_ms": "GCT vs Flight Timing",
    "impact_loading_rate_bw_s": "Impact Loading Rate",
    "trunk_forward_lean_deg": "Trunk Forward Lean",
    "left_right_asymmetry_pct": "Left/Right Asymmetry",
    "heel_strike_likelihood": "Foot Strike Signature",
}

COACHING_CUES = {
    "cadence_spm": "Cadence changed; try shorter, quicker steps without forcing pace.",
    "vertical_oscillation_cm": "Vertical bounce is influential; keep steps compact and quiet.",
    "gct_flight_balance_ms": "Ground-contact timing is influential; avoid heavy prolonged contact.",
    "impact_loading_rate_bw_s": "Impact proxy is influential; land softly under the body.",
    "trunk_forward_lean_deg": "Forward lean is influential; reset torso alignment from the ankles.",
    "left_right_asymmetry_pct": "Asymmetry proxy is influential; check belt fit and balanced steps.",
    "heel_strike_likelihood": "Foot-strike proxy is influential; confirm this cue with video or foot sensing.",
}

GOOD_FORM_CUE = "Form metrics are within the rule-based thresholds."

RULE_PRIORITY_SCORES = {
    1: 4.0,
    2: 3.0,
    3: 2.0,
    4: 1.0,
}

RULE_METRIC_TO_FEATURE = {
    "cadence_spm": "cadence_spm",
    "vertical_oscillation_cm": "vertical_oscillation_cm",
    "gct_ms": "gct_flight_balance_ms",
    "peak_vgrf_bw_estimate": "impact_loading_rate_bw_s",
    "trunk_forward_lean_deg": "trunk_forward_lean_deg",
    "left_right_asymmetry_pct": "left_right_asymmetry_pct",
    "footstrike_time_to_peak_ms": "heel_strike_likelihood",
}


@dataclass(frozen=True)
class IMUConfig:
    sample_rate_hz: int = 200
    body_frame_rotation: tuple[float, ...] = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    gravity_lowpass_hz: float = 0.5
    locomotor_band_hz: tuple[float, float] = (0.5, 5.0)
    impact_band_hz: tuple[float, float] = (5.0, 50.0)
    butterworth_order: int = 4
    window_s: float = 5.0
    stride_s: float = 1.0
    cadence_context_s: float = 10.0
    post_ic_window_s: float = 0.100
    footstrike_window_s: float = 0.050
    savgol_window_samples: int = 21
    savgol_polyorder: int = 5
    asymmetry_stride_window: int = 30
    min_running_cadence_spm: float = 100.0
    max_running_cadence_spm: float = 230.0

    @property
    def dt(self) -> float:
        return 1.0 / self.sample_rate_hz


@dataclass
class ProcessedIMU:
    timestamp_s: np.ndarray
    linear_z_locomotor_mps2: np.ndarray
    linear_z_impact_mps2: np.ndarray
    linear_z_wideband_mps2: np.ndarray
    vertical_velocity_mps: np.ndarray
    trunk_pitch_deg: np.ndarray


@dataclass
class StepEvent:
    ic_idx: int
    next_ic_idx: int
    side: int
    gct_ms: float
    flight_time_ms: float
    vertical_oscillation_cm: float
    peak_vgrf_bw: float
    loading_rate_bw_s: float
    crackle_rms: float
    asymmetry_pct: float
    heel_strike_likelihood: float
    footstrike_time_to_peak_ms: float


def _safe_sosfiltfilt(sos: np.ndarray, values: np.ndarray, axis: int = 0) -> np.ndarray:
    n = values.shape[axis]
    if n < 4:
        return values.copy()
    padlen = min(n - 1, max(1, 3 * (2 * len(sos) + 1)))
    return sosfiltfilt(sos, values, axis=axis, padlen=padlen)


def _integrate(values: np.ndarray, dt: float) -> np.ndarray:
    output = np.zeros_like(values, dtype=np.float64)
    if len(values) > 1:
        output[1:] = np.cumsum(0.5 * (values[1:] + values[:-1]) * dt)
    return output


def _remove_boundary_drift(values: np.ndarray) -> np.ndarray:
    return values - np.linspace(values[0], values[-1], len(values)) if len(values) > 1 else values.copy()


def _mean(values: Sequence[float], default: float) -> float:
    valid = np.asarray(values, dtype=np.float64)
    valid = valid[np.isfinite(valid)]
    return float(np.mean(valid)) if valid.size else default


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-float(np.clip(value, -60.0, 60.0))))


def _clamp(value: float, low: float, high: float) -> float:
    return float(np.clip(float(value), low, high))


def _softmax(values: np.ndarray) -> np.ndarray:
    shifted = values - float(np.max(values))
    exp_values = np.exp(shifted)
    return exp_values / max(float(np.sum(exp_values)), 1e-12)


def _logit(probability: float) -> float:
    value = float(np.clip(probability, 1e-8, 1.0 - 1e-8))
    return math.log(value / (1.0 - value))


class BiomechanicsExtractor:
    """Batch extraction matched to the supplied sacrum-mounted model pipeline."""

    def __init__(self, config: IMUConfig = IMUConfig(), neutral_pitch_deg: float = 0.0):
        self.config = config
        self.neutral_pitch_deg = neutral_pitch_deg
        self.auto_neutral_pitch_deg: float | None = None
        self.rotation = np.asarray(config.body_frame_rotation, dtype=np.float64).reshape(3, 3)

    def extract_latest(self, samples: Sequence[ImuSample]) -> dict[str, float] | None:
        output = self.extract_latest_with_diagnostics(samples)
        return output[0] if output is not None else None

    def extract_latest_with_diagnostics(
        self, samples: Sequence[ImuSample]
    ) -> tuple[dict[str, float], dict[str, float]] | None:
        if len(samples) < 3:
            return None
        time = np.asarray([sample.timestamp_s for sample in samples], dtype=np.float64)
        acc = np.asarray([[sample.acc_x_g, sample.acc_y_g, sample.acc_z_g] for sample in samples])
        gyro = np.asarray([[sample.gyro_x_dps, sample.gyro_y_dps, sample.gyro_z_dps] for sample in samples])
        time, acc, gyro = self._resample_uniform(time, acc, gyro)
        if len(time) < int(self.config.window_s * self.config.sample_rate_hz):
            return None
        processed = self._preprocess(time, acc, gyro)
        events = self._step_events(processed)
        return self._latest_window(processed, events)

    def _resample_uniform(
        self, timestamp: np.ndarray, acc: np.ndarray, gyro: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        order = np.argsort(timestamp)
        timestamp = timestamp[order]
        acc = acc[order]
        gyro = gyro[order]
        keep = np.concatenate(([True], np.diff(timestamp) > 1e-6))
        timestamp = timestamp[keep]
        acc = acc[keep]
        gyro = gyro[keep]
        if len(timestamp) < 3:
            return timestamp, acc, gyro

        end_s = float(timestamp[-1])
        start_s = max(float(timestamp[0]), end_s - self.config.cadence_context_s)
        if end_s - start_s < self.config.window_s:
            return timestamp, acc, gyro

        dt = self.config.dt
        uniform_time = np.arange(start_s, end_s + dt * 0.5, dt, dtype=np.float64)
        if len(uniform_time) < 3:
            return timestamp, acc, gyro
        uniform_acc = np.column_stack(
            [np.interp(uniform_time, timestamp, acc[:, axis]) for axis in range(3)]
        )
        uniform_gyro = np.column_stack(
            [np.interp(uniform_time, timestamp, gyro[:, axis]) for axis in range(3)]
        )
        return uniform_time, uniform_acc, uniform_gyro

    def _preprocess(self, timestamp: np.ndarray, acc: np.ndarray, gyro: np.ndarray) -> ProcessedIMU:
        fs = self.config.sample_rate_hz
        acc_body = acc @ self.rotation.T
        _gyro_body = gyro @ self.rotation.T
        gravity_sos = butter(self.config.butterworth_order, self.config.gravity_lowpass_hz, "lowpass", fs=fs, output="sos")
        loc_sos = butter(self.config.butterworth_order, self.config.locomotor_band_hz, "bandpass", fs=fs, output="sos")
        impact_sos = butter(self.config.butterworth_order, self.config.impact_band_hz, "bandpass", fs=fs, output="sos")
        gravity = _safe_sosfiltfilt(gravity_sos, acc_body, axis=0)
        linear_z = (acc_body[:, 2] - gravity[:, 2]) * G_TO_MPS2
        locomotor = _safe_sosfiltfilt(loc_sos, linear_z)
        impact = _safe_sosfiltfilt(impact_sos, linear_z)
        velocity = _remove_boundary_drift(_integrate(locomotor - np.mean(locomotor), self.config.dt))
        pitch = np.degrees(np.arctan2(gravity[:, 0], gravity[:, 2]))
        pitch = _safe_sosfiltfilt(gravity_sos, pitch)
        if abs(self.neutral_pitch_deg) < 1e-6 and self.auto_neutral_pitch_deg is None and len(pitch) >= int(2.0 * fs):
            self.auto_neutral_pitch_deg = float(np.median(pitch[: int(2.0 * fs)]))
        pitch = pitch - self.neutral_pitch_deg - (self.auto_neutral_pitch_deg or 0.0)
        return ProcessedIMU(timestamp, locomotor, impact, linear_z - np.mean(linear_z), velocity, pitch)

    def _step_events(self, data: ProcessedIMU) -> list[StepEvent]:
        fs = self.config.sample_rate_hz
        energy = np.abs(data.linear_z_impact_mps2)
        distance = max(1, int(0.25 * fs))
        prominence = max(0.6, float(np.percentile(energy, 90) * 0.35))
        peaks, _ = find_peaks(energy, distance=distance, prominence=prominence)
        if len(peaks) < 3:
            locomotor = data.linear_z_locomotor_mps2 - np.mean(data.linear_z_locomotor_mps2)
            peaks, _ = find_peaks(locomotor, distance=distance, prominence=max(0.2, float(np.std(locomotor) * 0.4)))
            energy = np.maximum(locomotor, 0.0)
        pairs: list[tuple[int, int]] = []
        previous_ic = -distance
        for peak in peaks:
            if energy[peak] <= 0:
                continue
            start = max(0, int(peak) - int(0.080 * fs))
            segment = energy[start : int(peak) + 1]
            below = np.flatnonzero(segment <= 0.50 * energy[peak])
            ic_idx = start + int(below[-1]) if below.size else start
            if ic_idx - previous_ic >= distance:
                pairs.append((ic_idx, int(peak)))
                previous_ic = ic_idx
        crackle = self._crackle(data.linear_z_wideband_mps2)
        events: list[StepEvent] = []
        for event_number, (ic_idx, _) in enumerate(pairs[:-1]):
            next_ic = pairs[event_number + 1][0]
            to_idx = self._toe_off(data.vertical_velocity_mps, ic_idx, next_ic)
            post = data.linear_z_wideband_mps2[ic_idx : min(len(data.timestamp_s), ic_idx + int(0.1 * fs))]
            peak_rel = int(np.argmax(post)) if post.size else 0
            peak_g = max(0.0, float(post[peak_rel] / G_TO_MPS2)) if post.size else 0.0
            peak_bw = float(np.clip(1.0 + peak_g, 0.5, 6.0))
            load_rate = max(0.0, (peak_bw - 1.0) / max(peak_rel / fs, 1.0 / fs))
            crackle_slice = crackle[ic_idx : min(len(crackle), ic_idx + int(0.1 * fs))]
            crackle_rms = float(np.sqrt(np.mean(np.square(crackle_slice)))) if crackle_slice.size else 0.0
            heel_likelihood, time_to_peak_ms = self._footstrike_signature(data.linear_z_impact_mps2, ic_idx)
            events.append(
                StepEvent(
                    ic_idx=ic_idx,
                    next_ic_idx=next_ic,
                    side=event_number % 2,
                    gct_ms=1000.0 * (to_idx - ic_idx) / fs,
                    flight_time_ms=1000.0 * max(0, next_ic - to_idx) / fs,
                    vertical_oscillation_cm=self._vertical_oscillation(data.linear_z_locomotor_mps2[ic_idx:next_ic]),
                    peak_vgrf_bw=peak_bw,
                    loading_rate_bw_s=float(load_rate),
                    crackle_rms=crackle_rms,
                    asymmetry_pct=0.0,
                    heel_strike_likelihood=heel_likelihood,
                    footstrike_time_to_peak_ms=time_to_peak_ms,
                )
            )
        for index, event in enumerate(events):
            history = events[max(0, index - self.config.asymmetry_stride_window + 1) : index + 1]
            left = [item.crackle_rms for item in history if item.side == 0]
            right = [item.crackle_rms for item in history if item.side == 1]
            denominator = max(1e-8, 0.5 * (_mean(left, event.crackle_rms) + _mean(right, event.crackle_rms)))
            event.asymmetry_pct = 100.0 * abs(_mean(left, event.crackle_rms) - _mean(right, event.crackle_rms)) / denominator
        return events

    def _toe_off(self, velocity: np.ndarray, ic_idx: int, next_ic: int) -> int:
        fs = self.config.sample_rate_hz
        start = min(next_ic - 2, ic_idx + int(0.070 * fs))
        stop = max(start + 2, next_ic - int(0.035 * fs))
        segment = velocity[start:stop]
        crossings = np.flatnonzero((segment[:-1] < 0.0) & (segment[1:] >= 0.0))
        return int(start + crossings[0] + 1) if crossings.size else int(ic_idx + 0.62 * (next_ic - ic_idx))

    def _vertical_oscillation(self, acceleration: np.ndarray) -> float:
        if len(acceleration) < 4:
            return 0.0
        velocity = _remove_boundary_drift(_integrate(acceleration - np.mean(acceleration), self.config.dt))
        displacement = _remove_boundary_drift(_integrate(velocity - np.mean(velocity), self.config.dt))
        return float((np.max(displacement) - np.min(displacement)) * 100.0)

    def _crackle(self, values: np.ndarray) -> np.ndarray:
        window = self.config.savgol_window_samples + (self.config.savgol_window_samples % 2 == 0)
        if len(values) > window:
            return savgol_filter(values, window, self.config.savgol_polyorder, deriv=3, delta=self.config.dt, mode="interp")
        return np.gradient(np.gradient(np.gradient(values, self.config.dt), self.config.dt), self.config.dt)

    def _footstrike_signature(self, impact: np.ndarray, ic_idx: int) -> tuple[float, float]:
        stop = min(len(impact), ic_idx + int(self.config.footstrike_window_s * self.config.sample_rate_hz))
        segment = np.abs(impact[ic_idx:stop]) / G_TO_MPS2
        if not segment.size:
            return 0.5, 0.0
        peak_idx = int(np.argmax(segment))
        peak_g = float(segment[peak_idx])
        time_to_peak = max(1.0 / self.config.sample_rate_hz, peak_idx / self.config.sample_rate_hz)
        sharpness = peak_g / time_to_peak
        likelihood = _sigmoid(-1.25 + 1.8 * peak_g / 1.2 - 1.1 * time_to_peak / 0.030 + 0.55 * sharpness / 80.0)
        return likelihood, time_to_peak * 1000.0

    def _cadence(self, signal: np.ndarray) -> float:
        fs = self.config.sample_rate_hz
        if len(signal) < int(1.5 * fs):
            return 0.0
        values = signal - np.mean(signal)
        correlation = np.correlate(values, values, mode="full")[len(values) - 1 :]
        if correlation[0] <= 1e-8:
            return 0.0
        correlation /= correlation[0]
        min_lag = int(fs * 60.0 / self.config.max_running_cadence_spm)
        max_lag = min(len(correlation) - 1, int(fs * 60.0 / self.config.min_running_cadence_spm))
        if max_lag <= min_lag:
            return 0.0
        lag = min_lag + int(np.argmax(correlation[min_lag : max_lag + 1]))
        return float(60.0 * fs / lag) if correlation[lag] >= 0.05 else 0.0

    def _latest_window(
        self, data: ProcessedIMU, events: Sequence[StepEvent]
    ) -> tuple[dict[str, float], dict[str, float]] | None:
        window_samples = int(self.config.window_s * self.config.sample_rate_hz)
        if len(data.timestamp_s) < window_samples:
            return None
        start = len(data.timestamp_s) - window_samples
        in_window = [event for event in events if start <= event.ic_idx < len(data.timestamp_s)]
        cadence_samples = int(self.config.cadence_context_s * self.config.sample_rate_hz)
        cadence = self._cadence(data.linear_z_locomotor_mps2[-cadence_samples:])
        if len(in_window) < 2 or not self.config.min_running_cadence_spm <= cadence <= self.config.max_running_cadence_spm:
            return self._fallback_window(data, start, cadence, len(in_window))
        gct = _mean([event.gct_ms for event in in_window], 0.0)
        flight = _mean([event.flight_time_ms for event in in_window], 0.0)
        features = {
            "cadence_spm": cadence,
            "vertical_oscillation_cm": _mean([event.vertical_oscillation_cm for event in in_window], 0.0),
            "gct_flight_balance_ms": gct - flight,
            "impact_loading_rate_bw_s": _mean([event.loading_rate_bw_s for event in in_window], 0.0),
            "trunk_forward_lean_deg": float(np.median(data.trunk_pitch_deg[start:])),
            "left_right_asymmetry_pct": _mean([event.asymmetry_pct for event in in_window], 0.0),
            "heel_strike_likelihood": _mean([event.heel_strike_likelihood for event in in_window], 0.5),
        }
        diagnostics = {
            "gct_ms": gct,
            "flight_time_ms": flight,
            "peak_vgrf_bw_estimate": _mean([event.peak_vgrf_bw for event in in_window], 1.0),
            "footstrike_time_to_peak_ms": _mean(
                [event.footstrike_time_to_peak_ms for event in in_window], 0.0
            ),
        }
        return features, diagnostics

    def _fallback_window(
        self, data: ProcessedIMU, start: int, cadence: float, event_count: int
    ) -> tuple[dict[str, float], dict[str, float]]:
        """Emit dashboard-safe features even when step detection is not confident yet."""

        fs = self.config.sample_rate_hz
        locomotor = data.linear_z_locomotor_mps2[start:]
        impact = data.linear_z_impact_mps2[start:]
        wideband = data.linear_z_wideband_mps2[start:]
        pitch = data.trunk_pitch_deg[start:]

        rise_rates: list[float] = []
        if len(wideband) > 1:
            rise = np.diff(wideband / G_TO_MPS2) * fs
            rise_rates = [float(value) for value in rise if np.isfinite(value) and value > 0.0]

        peak_g = float(np.max(np.abs(wideband)) / G_TO_MPS2) if len(wideband) else 0.0
        impact_peak_g = float(np.max(np.abs(impact)) / G_TO_MPS2) if len(impact) else peak_g
        loading_rate = max(rise_rates, default=0.0)
        heel_likelihood = _sigmoid(-1.25 + 1.8 * impact_peak_g / 1.2 + 0.55 * loading_rate / 80.0)

        features = {
            "cadence_spm": _clamp(cadence, 0.0, 260.0),
            "vertical_oscillation_cm": _clamp(self._vertical_oscillation(locomotor), 0.0, 40.0),
            "gct_flight_balance_ms": 0.0,
            "impact_loading_rate_bw_s": _clamp(loading_rate, 0.0, 100.0),
            "trunk_forward_lean_deg": _clamp(float(np.median(pitch)) if len(pitch) else 0.0, -45.0, 45.0),
            "left_right_asymmetry_pct": 0.0,
            "heel_strike_likelihood": _clamp(heel_likelihood, 0.0, 1.0),
        }
        diagnostics = {
            "gct_ms": 0.0,
            "flight_time_ms": 0.0,
            "peak_vgrf_bw_estimate": _clamp(1.0 + peak_g, 0.5, 6.0),
            "footstrike_time_to_peak_ms": 0.0,
            "fallback_window": 1.0,
            "detected_step_events": float(event_count),
        }
        return features, diagnostics


def _load_interpreter_class():
    try:
        from ai_edge_litert.interpreter import Interpreter

        return Interpreter
    except ImportError:
        try:
            from tflite_runtime.interpreter import Interpreter

            return Interpreter
        except ImportError as error:
            raise RuntimeError(
                "LiteRT runtime is not installed. Install python/requirements_unoq.txt on UNO Q."
            ) from error


class _JsonXGBoostBinaryClassifier:
    """Small XGBoost JSON evaluator for binary:logistic tree ensembles."""

    def __init__(self, model_path: Path):
        model = json.loads(model_path.read_text(encoding="utf-8"))
        learner = model["learner"]
        self.trees = learner["gradient_booster"]["model"]["trees"]
        parameters = learner.get("learner_model_param", {})
        raw_base_score = str(parameters.get("base_score", "0.5")).strip("[]")
        self.base_margin = _logit(float(raw_base_score))

    @staticmethod
    def _node_value(tree: Mapping[str, object], node: int) -> float:
        return float(tree["base_weights"][node])  # type: ignore[index]

    def _tree_margin_and_contributions(self, tree: Mapping[str, object], values: np.ndarray) -> tuple[float, np.ndarray]:
        left = tree["left_children"]  # type: ignore[index]
        right = tree["right_children"]  # type: ignore[index]
        split_indices = tree["split_indices"]  # type: ignore[index]
        split_conditions = tree["split_conditions"]  # type: ignore[index]
        default_left = tree.get("default_left", [])  # type: ignore[assignment]
        contributions = np.zeros(values.shape[0], dtype=np.float64)
        node = 0

        while int(left[node]) != -1 or int(right[node]) != -1:
            feature_index = int(split_indices[node])
            threshold = float(split_conditions[node])
            feature_value = values[feature_index]
            if not np.isfinite(feature_value):
                go_left = bool(default_left[node]) if node < len(default_left) else True
            else:
                go_left = bool(feature_value < threshold)
            child = int(left[node] if go_left else right[node])
            contributions[feature_index] += self._node_value(tree, child) - self._node_value(tree, node)
            node = child

        return self._node_value(tree, node), contributions

    def predict(self, values: np.ndarray) -> tuple[float, np.ndarray]:
        margin = self.base_margin
        contributions = np.zeros(values.shape[0], dtype=np.float64)
        for tree in self.trees:
            tree_margin, tree_contributions = self._tree_margin_and_contributions(tree, values)
            margin += tree_margin
            contributions += tree_contributions
        probability_bad = _sigmoid(margin)
        return probability_bad, contributions


class RunningFormPredictor:
    """Runs the deployed model contract, backed by TFLite, XGBoost, or rules."""

    def __init__(self, model_path: Path, normalizer_path: Path | None = None):
        self.model_path = model_path
        self.model_kind = model_path.suffix.lower().lstrip(".")
        self.feature_columns = list(FEATURE_KEYS)
        self.class_names = ["Good", "Bad Form"]
        self.normalizer: Mapping[str, object] | None = None
        self.rule_engine: MetricTriggerEngine | None = None
        metadata_path = model_path.with_name(f"{model_path.stem}_metadata.json")
        if metadata_path.exists():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.feature_columns = list(metadata.get("feature_columns", self.feature_columns))
            self.class_names = list(metadata.get("class_names", self.class_names))
        if self.model_kind == "json":
            model_metadata = json.loads(model_path.read_text(encoding="utf-8"))
            if model_metadata.get("model_type") == "rulebase":
                self.model_kind = "rulebase"
                self.feature_columns = list(model_metadata.get("feature_columns", self.feature_columns))
                self.class_names = list(model_metadata.get("class_names", self.class_names))
                self.rule_engine = MetricTriggerEngine()
                return
        if normalizer_path is not None and not normalizer_path.exists():
            raise FileNotFoundError(f"normalizer file not found: {normalizer_path}")
        if normalizer_path is not None:
            self.normalizer = json.loads(normalizer_path.read_text(encoding="utf-8"))
            normalized_columns = self.normalizer.get("feature_columns", self.feature_columns)
            if list(normalized_columns) != list(self.feature_columns):
                raise ValueError("normalizer feature_columns do not match model feature_columns")
        if self.model_kind == "json":
            self.xgboost_model = _JsonXGBoostBinaryClassifier(model_path)
            return
        if self.model_kind == "tflite":
            Interpreter = _load_interpreter_class()
            self.interpreter = Interpreter(model_path=str(model_path))
            self.interpreter.allocate_tensors()
            signatures = self.interpreter.get_signature_list()
            if "serving_default" not in signatures:
                raise ValueError("TFLite model is missing serving_default signature")
            signature = signatures["serving_default"]
            expected_outputs = {"feature_attention", "probabilities"}
            if "features" not in signature["inputs"] or not expected_outputs.issubset(signature["outputs"]):
                raise ValueError("TFLite model must expose features input and feature_attention/probabilities outputs")
            self.runner = self.interpreter.get_signature_runner("serving_default")
            return
        raise ValueError(f"Unsupported model type: {model_path}")

    @property
    def production_ready(self) -> bool:
        return self.model_kind == "rulebase" or self.normalizer is not None

    def _rulebase_prediction(
        self, features: Mapping[str, float], diagnostics: Mapping[str, float] | None
    ) -> dict[str, object]:
        if self.rule_engine is None:
            raise RuntimeError("rule-based predictor was not initialized")

        triggers = self.rule_engine.evaluate(features, diagnostics or {})
        contributions = np.zeros(len(self.feature_columns), dtype=np.float64)
        scores = np.zeros(len(self.feature_columns), dtype=np.float64)
        for trigger in triggers:
            feature = RULE_METRIC_TO_FEATURE.get(trigger.metric)
            if feature not in self.feature_columns:
                continue
            index = self.feature_columns.index(feature)
            score = RULE_PRIORITY_SCORES.get(trigger.priority, 1.0)
            scores[index] = max(scores[index], score)
            contributions[index] += score

        if float(np.sum(scores)) > 1e-8:
            weights = scores / float(np.sum(scores))
            dominant = self.feature_columns[int(np.argmax(scores))]
        else:
            weights = np.full(len(self.feature_columns), 1.0 / len(self.feature_columns), dtype=np.float64)
            dominant = self.feature_columns[0]

        bad_form = bool(triggers)
        probabilities = np.asarray([0.0, 1.0] if bad_form else [1.0, 0.0], dtype=np.float32)
        priority_trigger = triggers[0] if triggers else None
        if priority_trigger is not None:
            dominant = RULE_METRIC_TO_FEATURE.get(priority_trigger.metric, dominant)
            if dominant not in self.feature_columns:
                dominant = self.feature_columns[int(np.argmax(weights))]
        predicted_index = 1 if bad_form else 0
        contribution_map = {
            key: round(float(value), 6) for key, value in zip(self.feature_columns, contributions)
        }
        return {
            "class": self.class_names[predicted_index],
            "probabilities": {name: round(float(probabilities[index]), 6) for index, name in enumerate(self.class_names)},
            "attention_weights": {key: round(float(value), 6) for key, value in zip(self.feature_columns, weights)},
            "feature_contributions": contribution_map,
            "dominant_feature": dominant,
            "dominant_feature_display": FEATURE_DISPLAY_NAMES.get(dominant, dominant),
            "coaching_cue": priority_trigger.message_th if priority_trigger else GOOD_FORM_CUE,
            "production_ready": self.production_ready,
            "model_type": "rulebase",
            "normalization_status": "NOT_REQUIRED_RULE_BASE",
        }

    def predict(
        self, features: Mapping[str, float], diagnostics: Mapping[str, float] | None = None
    ) -> dict[str, object]:
        if self.model_kind == "rulebase":
            return self._rulebase_prediction(features, diagnostics)

        raw_values = np.asarray([features[key] for key in self.feature_columns], dtype=np.float32)
        values = raw_values.reshape(1, -1)
        if self.normalizer is not None:
            mean = np.asarray(self.normalizer["mean"], dtype=np.float32)
            std = np.maximum(np.asarray(self.normalizer["std"], dtype=np.float32), 1e-6)
            values = (values - mean) / std

        if self.model_kind == "json":
            probability_bad, contributions = self.xgboost_model.predict(values[0].astype(np.float64))
            probabilities = np.asarray([1.0 - probability_bad, probability_bad], dtype=np.float32)
            abs_contributions = np.abs(contributions)
            if float(np.sum(abs_contributions)) > 1e-8:
                weights = abs_contributions / float(np.sum(abs_contributions))
            else:
                weights = np.full(len(self.feature_columns), 1.0 / len(self.feature_columns), dtype=np.float64)
            contribution_map = {
                key: round(float(value), 6) for key, value in zip(self.feature_columns, contributions)
            }
            normalization_status = "LOADED" if self.production_ready else "MISSING_XGBOOST_NORMALIZER"
        else:
            outputs = self.runner(features=values)
            probabilities = np.asarray(outputs["probabilities"], dtype=np.float32)[0]
            weights = np.asarray(outputs["feature_attention"], dtype=np.float32)[0]
            weights = weights / max(float(np.sum(weights)), 1e-8)
            contribution_map = {
                key: round(float(value), 6)
                for key, value in zip(self.feature_columns, np.log(np.maximum(probabilities, 1e-8))[int(np.argmax(probabilities))] * weights)
            }
            normalization_status = "LOADED" if self.production_ready else "MISSING_TRAINING_NORMALIZER"

        dominant = self.feature_columns[int(np.argmax(weights))]
        predicted_index = int(np.argmax(probabilities))
        return {
            "class": self.class_names[predicted_index],
            "probabilities": {name: round(float(probabilities[index]), 6) for index, name in enumerate(self.class_names)},
            "attention_weights": {key: round(float(value), 6) for key, value in zip(self.feature_columns, weights)},
            "feature_contributions": contribution_map,
            "dominant_feature": dominant,
            "dominant_feature_display": FEATURE_DISPLAY_NAMES.get(dominant, dominant),
            "coaching_cue": COACHING_CUES[dominant],
            "production_ready": self.production_ready,
            "model_type": "xgboost" if self.model_kind == "json" else "tflite",
            "normalization_status": normalization_status,
        }
