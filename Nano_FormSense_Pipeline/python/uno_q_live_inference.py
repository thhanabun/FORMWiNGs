#!/usr/bin/env python3
"""Run Nano -> UNO Q LiteRT running-form inference and attention output."""

from __future__ import annotations

import argparse
import csv
import json
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from formsense_pipeline.bluetooth_delivery import BluetoothAlertDelivery
from formsense_pipeline.filters import Calibration
from formsense_pipeline.metric_triggers import MetricTriggerEngine
from formsense_pipeline.protocol import FEATURE_KEYS, RAW_COLUMNS, ImuSample, ProtocolError, encode_alert, parse_imu
from formsense_pipeline.unoq_model import BiomechanicsExtractor, IMUConfig, RunningFormPredictor

DEFAULT_MODEL = Path(__file__).resolve().parents[1] / "model" / "running_form_transformer_fp16.tflite"


def _csv_samples(path: Path) -> Iterator[ImuSample]:
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            yield ImuSample(**{key: int(row[key]) if key == "seq" else float(row[key]) for key in RAW_COLUMNS})


def _serial_samples(port: str, baud: int):
    try:
        import serial
    except ImportError as error:
        raise SystemExit("Install pyserial before using --port: pip install pyserial") from error
    with serial.Serial(port, baud, timeout=0.2) as uart:
        while True:
            line = uart.readline().decode("ascii", errors="ignore").strip()
            if not line:
                continue
            try:
                yield parse_imu(line), uart
            except ProtocolError as error:
                print(f"dropped invalid UART packet: {error}")


class UNOQInferenceSession:
    def __init__(
        self,
        output_dir: Path,
        session_id: str,
        predictor: RunningFormPredictor,
        calibration: Calibration,
        config: IMUConfig,
        warmup_s: float,
        bad_form_threshold: float,
        cooldown_s: float,
        allow_demo_alerts: bool,
        enable_metric_alerts: bool,
        delivery: BluetoothAlertDelivery,
    ):
        output_dir.mkdir(parents=True, exist_ok=True)
        self.raw_path = output_dir / f"{session_id}_unoq_raw.csv"
        self.features_path = output_dir / f"{session_id}_unoq_features.csv"
        self.predictions_path = output_dir / f"{session_id}_predictions.jsonl"
        self.predictor = predictor
        # Collector calibration uses atan2(-ax, ...); the reference model uses atan2(+ax, ...).
        self.extractor = BiomechanicsExtractor(config, neutral_pitch_deg=-calibration.neutral_pitch_deg)
        self.config = config
        self.warmup_s = warmup_s
        self.bad_form_threshold = bad_form_threshold
        self.cooldown_s = cooldown_s
        self.allow_demo_alerts = allow_demo_alerts
        self.enable_metric_alerts = enable_metric_alerts
        self.trigger_engine = MetricTriggerEngine()
        self.delivery = delivery
        self.samples: deque[ImuSample] = deque()
        self.window_id = 0
        self.next_emit_s: float | None = None
        self.last_alert_s = float("-inf")
        self.previous_seq: int | None = None
        self.rate_check_timestamps: list[float] = []
        self.rate_warning_reported = False
        self.raw_handle = self.raw_path.open("w", newline="", encoding="utf-8", buffering=1)
        self.features_handle = self.features_path.open("w", newline="", encoding="utf-8", buffering=1)
        self.predictions_handle = self.predictions_path.open("w", encoding="utf-8", buffering=1)
        self.raw_writer = csv.DictWriter(self.raw_handle, fieldnames=RAW_COLUMNS)
        self.raw_writer.writeheader()
        feature_fields = [
            "window_id",
            "timestamp_s",
            *FEATURE_KEYS,
            "predicted_class",
            "p_good",
            "p_bad_form",
            "dominant_feature",
            "production_ready",
            "gct_ms",
            "peak_vgrf_bw_estimate",
            "footstrike_time_to_peak_ms",
            "priority_trigger",
        ]
        self.feature_writer = csv.DictWriter(self.features_handle, fieldnames=feature_fields)
        self.feature_writer.writeheader()

    def close(self) -> None:
        self.raw_handle.close()
        self.features_handle.close()
        self.predictions_handle.close()

    def ingest(self, sample: ImuSample) -> tuple[dict[str, object] | None, str | None]:
        self.raw_writer.writerow(sample.__dict__)
        if self.previous_seq is not None and sample.seq != (self.previous_seq + 1) & 0xFFFFFFFF:
            print(f"WARNING packet gap: expected {(self.previous_seq + 1) & 0xFFFFFFFF}, received {sample.seq}")
        self.previous_seq = sample.seq
        if len(self.rate_check_timestamps) < self.config.sample_rate_hz + 1:
            self.rate_check_timestamps.append(sample.timestamp_s)
        elif not self.rate_warning_reported:
            duration = self.rate_check_timestamps[-1] - self.rate_check_timestamps[0]
            measured_rate = (len(self.rate_check_timestamps) - 1) / duration if duration > 0 else 0.0
            if abs(measured_rate - self.config.sample_rate_hz) > self.config.sample_rate_hz * 0.05:
                print(
                    f"WARNING measured input rate {measured_rate:.1f} Hz does not match "
                    f"model configuration {self.config.sample_rate_hz} Hz."
                )
            self.rate_warning_reported = True
        self.samples.append(sample)
        cutoff = sample.timestamp_s - self.config.cadence_context_s
        while self.samples and self.samples[0].timestamp_s < cutoff:
            self.samples.popleft()
        if self.next_emit_s is None:
            self.next_emit_s = sample.timestamp_s + self.warmup_s
        if sample.timestamp_s < self.next_emit_s:
            return None, None
        self.next_emit_s += self.config.stride_s
        extraction = self.extractor.extract_latest_with_diagnostics(list(self.samples))
        if extraction is None:
            return None, None
        features, diagnostics = extraction
        self.window_id += 1
        prediction = self.predictor.predict(features)
        triggers = self.trigger_engine.evaluate(features, diagnostics)
        actionable = next((trigger for trigger in triggers if trigger.severity in {"ALERT", "WARN"}), None)
        payload: dict[str, object] = {
            "type": "running_form_prediction",
            "window_id": self.window_id,
            "timestamp_s": round(sample.timestamp_s, 4),
            "features": {key: round(float(features[key]), 5) for key in FEATURE_KEYS},
            "diagnostics": {key: round(float(value), 5) for key, value in diagnostics.items()},
            "metric_triggers": [trigger.as_dict() for trigger in triggers],
            "priority_trigger": actionable.as_dict() if actionable else None,
            **prediction,
        }
        probabilities = prediction["probabilities"]
        self.feature_writer.writerow(
            {
                "window_id": self.window_id,
                "timestamp_s": round(sample.timestamp_s, 4),
                **{key: round(float(features[key]), 5) for key in FEATURE_KEYS},
                "predicted_class": prediction["class"],
                "p_good": probabilities.get("Good", 0.0),
                "p_bad_form": probabilities.get("Bad Form", 0.0),
                "dominant_feature": prediction["dominant_feature"],
                "production_ready": prediction["production_ready"],
                "gct_ms": round(diagnostics["gct_ms"], 5),
                "peak_vgrf_bw_estimate": round(diagnostics["peak_vgrf_bw_estimate"], 5),
                "footstrike_time_to_peak_ms": round(diagnostics["footstrike_time_to_peak_ms"], 5),
                "priority_trigger": actionable.code if actionable else "",
            }
        )
        alert: str | None = None
        model_alerts_enabled = self.predictor.production_ready or self.allow_demo_alerts
        bad_form_detected = probabilities.get("Bad Form", 0.0) >= self.bad_form_threshold
        metric_triggered = self.enable_metric_alerts and actionable is not None
        model_triggered = model_alerts_enabled and bad_form_detected
        if (
            (metric_triggered or model_triggered)
            and sample.timestamp_s - self.last_alert_s >= self.cooldown_s
        ):
            severity = actionable.severity if metric_triggered else "ALERT"
            message = actionable.device_message if metric_triggered else str(prediction["coaching_cue"])
            alert = encode_alert(severity, sample.timestamp_s, message)
            self.last_alert_s = sample.timestamp_s
        payload["feedback"] = {
            "bad_form_threshold": self.bad_form_threshold,
            "bad_form_detected": bad_form_detected,
            "alert_triggered": alert is not None,
            "alert_source": "METRIC_TRIGGER" if alert and metric_triggered else ("MODEL" if alert else None),
            "model_alerts_enabled": model_alerts_enabled,
            "metric_alerts_enabled": self.enable_metric_alerts,
            "disabled_reason": None
            if (model_alerts_enabled or self.enable_metric_alerts)
            else "MISSING_TRAINING_NORMALIZER_AND_METRIC_ALERTS_DISABLED",
        }
        if alert is not None:
            payload["feedback"]["bluetooth_delivery"] = self.delivery.deliver_prediction(payload)
        self.predictions_handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        return payload, alert


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="UNO Q Linux streaming inference from Nano waist IMU data.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--port", help="Nano USB/UART serial port on UNO Q, e.g. /dev/ttyACM0.")
    source.add_argument("--replay-csv", type=Path, help="Raw Nano CSV for hardware-free replay.")
    parser.add_argument("--baud", type=int, default=460800)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--normalizer", type=Path, help="running_form_normalizer.json saved during model training.")
    parser.add_argument("--calibration", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path.home() / "formsense_data" / "live")
    parser.add_argument("--session-id", default=datetime.now(timezone.utc).strftime("unoq_%Y%m%dT%H%M%SZ"))
    parser.add_argument("--sample-rate-hz", type=int, default=200)
    parser.add_argument(
        "--body-frame-rotation",
        type=float,
        nargs=9,
        default=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
        metavar=("R00", "R01", "R02", "R10", "R11", "R12", "R20", "R21", "R22"),
        help="3x3 row-major rotation from mounted sensor axes to X-forward Y-left Z-up.",
    )
    parser.add_argument("--warmup-s", type=float, default=10.0)
    parser.add_argument("--bad-form-threshold", type=float, default=0.70)
    parser.add_argument("--cooldown-s", type=float, default=20.0)
    parser.add_argument("--allow-demo-alerts", action="store_true", help="Send alerts despite missing training normalizer.")
    parser.add_argument(
        "--enable-metric-alerts",
        action="store_true",
        help="Allow WARN/ALERT priority metric triggers to send @ALERT without model normalization.",
    )
    parser.add_argument("--ble-address", help="BLE target device address/identifier that exposes a writable alert characteristic.")
    parser.add_argument("--ble-characteristic", help="Writable BLE GATT characteristic UUID for compact alert JSON.")
    parser.add_argument("--ble-scan-timeout-s", type=float, default=3.0)
    parser.add_argument(
        "--uart-feedback",
        action="store_true",
        help="Also send @ALERT back through the Nano serial cable; Bluetooth/local-outbox remains primary.",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    if not args.model.exists():
        raise SystemExit(f"Model file not found: {args.model}")
    predictor = RunningFormPredictor(args.model, args.normalizer)
    if not predictor.production_ready:
        print("WARNING: training normalizer not supplied; predictions/attention are integration-demo output only.")
        print("Model-driven alerts are disabled unless --allow-demo-alerts is explicitly passed.")
        if args.enable_metric_alerts:
            print("Rule-based metric alerts are explicitly enabled; validate thresholds before runner use.")
    delivery = BluetoothAlertDelivery(
        output_dir=args.output_dir,
        session_id=args.session_id,
        address=args.ble_address,
        characteristic=args.ble_characteristic,
        scan_timeout_s=args.ble_scan_timeout_s,
    )
    if args.ble_address or args.ble_characteristic:
        if not delivery.configured:
            raise SystemExit("Both --ble-address and --ble-characteristic are required for BLE delivery.")
        retry = delivery.retry_pending()
        print(f"BLE delivery configured; retried={retry['sent']} pending={retry['remaining']}")
    else:
        print("BLE not configured; generated alerts will be stored in the local outbox.")
    session = UNOQInferenceSession(
        output_dir=args.output_dir,
        session_id=args.session_id,
        predictor=predictor,
        calibration=Calibration.load(args.calibration),
        config=IMUConfig(sample_rate_hz=args.sample_rate_hz, body_frame_rotation=tuple(args.body_frame_rotation)),
        warmup_s=args.warmup_s,
        bad_form_threshold=args.bad_form_threshold,
        cooldown_s=args.cooldown_s,
        allow_demo_alerts=args.allow_demo_alerts,
        enable_metric_alerts=args.enable_metric_alerts,
        delivery=delivery,
    )
    print(f"Model: {args.model}")
    print(f"Saving: {session.raw_path}, {session.features_path}, {session.predictions_path}")
    try:
        if args.replay_csv:
            for sample in _csv_samples(args.replay_csv):
                payload, _ = session.ingest(sample)
                if payload:
                    print(json.dumps(payload, ensure_ascii=False))
        else:
            for sample, uart in _serial_samples(args.port, args.baud):
                payload, alert = session.ingest(sample)
                if payload:
                    print(json.dumps(payload, ensure_ascii=False))
                if alert:
                    if args.uart_feedback:
                        uart.write((alert + "\n").encode("ascii"))
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        session.close()


if __name__ == "__main__":
    main()
