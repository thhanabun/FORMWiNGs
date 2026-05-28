#!/usr/bin/env python3
"""Run FormSense inference from UNO Q MCU RouterBridge batches."""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from arduino.app_utils import App, Bridge

from formsense_pipeline.bluetooth_delivery import BluetoothAlertDelivery
from formsense_pipeline.filters import Calibration
from formsense_pipeline.protocol import ProtocolError, parse_imu
from formsense_pipeline.unoq_model import IMUConfig, RunningFormPredictor
from uno_q_live_inference import DEFAULT_MODEL, UNOQInferenceSession


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="UNO Q RouterBridge inference from MCU UART batches.")
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--normalizer", type=Path)
    parser.add_argument("--calibration", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path.home() / "formsense_data" / "live")
    parser.add_argument("--session-id", default=datetime.now(timezone.utc).strftime("bridge_%Y%m%dT%H%M%SZ"))
    parser.add_argument("--sample-rate-hz", type=int, default=200)
    parser.add_argument(
        "--body-frame-rotation",
        type=float,
        nargs=9,
        default=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
        metavar=("R00", "R01", "R02", "R10", "R11", "R12", "R20", "R21", "R22"),
    )
    parser.add_argument("--warmup-s", type=float, default=10.0)
    parser.add_argument("--bad-form-threshold", type=float, default=0.70)
    parser.add_argument("--cooldown-s", type=float, default=20.0)
    parser.add_argument("--allow-demo-alerts", action="store_true")
    parser.add_argument("--enable-metric-alerts", action="store_true")
    parser.add_argument("--ble-address")
    parser.add_argument("--ble-characteristic")
    parser.add_argument("--ble-scan-timeout-s", type=float, default=3.0)
    parser.add_argument("--worker-batch-size", type=int, default=80)
    parser.add_argument("--worker-batch-wait-s", type=float, default=0.05)
    parser.add_argument(
        "--mcu-ble",
        action="store_true",
        help="Send compact prediction JSON back to UNO Q MCU method formsense/ble_notify for BLE notify.",
    )
    parser.add_argument("--max-queue", type=int, default=2000)
    return parser


def _round_number(value: object, digits: int = 3) -> float:
    return round(float(value), digits)


def _rounded_mapping(values: object, digits: int = 3) -> dict[str, float]:
    if not isinstance(values, dict):
        return {}
    output: dict[str, float] = {}
    for key, value in values.items():
        try:
            output[str(key)] = _round_number(value, digits)
        except (TypeError, ValueError):
            continue
    return output


def _compact_trigger(trigger: object) -> dict[str, object]:
    if not isinstance(trigger, dict):
        return {}
    output: dict[str, object] = {}
    for key in ("priority", "severity", "code", "message_th"):
        if key not in trigger:
            continue
        value = trigger[key]
        if isinstance(value, (int, float)):
            output[key] = int(value) if key == "priority" else round(float(value), 3)
        else:
            output[key] = str(value)
    return output


def _model_output_mcu_ble_payload(prediction: dict[str, object]) -> str:
    """Return LightBlue-friendly model output JSON for MCU BLE notifications."""

    row: dict[str, object] = {
        "type": "running_form_prediction",
        "window_id": int(prediction.get("window_id", 0)),
        "timestamp_s": _round_number(prediction.get("timestamp_s", 0.0), 4),
        "features": _rounded_mapping(prediction.get("features"), 2),
        "diagnostics": _rounded_mapping(prediction.get("diagnostics"), 2),
        "class": str(prediction.get("class", "")),
        "probabilities": _rounded_mapping(prediction.get("probabilities"), 4),
        "attention_weights": _rounded_mapping(prediction.get("attention_weights"), 4),
        "dominant_feature": str(prediction.get("dominant_feature", "")),
        "priority_trigger": _compact_trigger(prediction.get("priority_trigger")),
    }

    return json.dumps(row, ensure_ascii=False, separators=(",", ":"))


def _send_mcu_payload(payload: dict[str, object]) -> dict[str, object]:
    """Send a JSON dashboard payload to the MCU BLE characteristic."""

    compact = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), default=str)
    print(f"MCU_BLE_PAYLOAD_JSON={compact}", flush=True)
    try:
        with BRIDGE_LOCK:
            if len(compact) <= 90:
                Bridge.call("formsense/ble_notify", compact)
                return {"status": "SENT_TO_MCU", "bytes": len(compact), "chunks": 1}

            Bridge.call("formsense/ble_begin", "1")
            chunks = [compact[index : index + 72] for index in range(0, len(compact), 72)]
            for chunk in chunks:
                Bridge.call("formsense/ble_chunk", chunk)
            Bridge.call("formsense/ble_commit", "1")
        return {"status": "SENT_TO_MCU", "bytes": len(compact), "chunks": len(chunks)}
    except Exception as error:
        return {"status": "ERROR", "reason": type(error).__name__, "message": str(error)}


def _bridge_status_payload(
    status: str,
    stats: dict[str, object],
    *,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    """Return a compact status payload for App Lab logs only."""

    payload: dict[str, object] = {
        "type": "bridge_status",
        "status": status,
        "rx": int(stats.get("received", 0)),
        "ok": int(stats.get("processed", 0)),
        "bad": int(stats.get("invalid", 0)),
        "q": int(extra.get("queue_size", 0) if extra else 0),
    }
    if extra:
        for key, value in extra.items():
            if key == "queue_size":
                continue
            text = str(value)
            payload[key] = text[:72] if len(text) > 72 else value
    return payload


def _log_bridge_status(
    status: str,
    stats: dict[str, object],
    *,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    payload = _bridge_status_payload(status, stats, extra=extra)
    print(f"BRIDGE_STATUS_JSON={json.dumps(payload, ensure_ascii=False, default=str)}", flush=True)
    return {"status": "LOGGED_ONLY", "type": "bridge_status"}


BRIDGE_LOCK = threading.Lock()


def main() -> None:
    args = _parser().parse_args()
    if not args.model.exists():
        raise SystemExit(f"Model file not found: {args.model}")
    if bool(args.ble_address) != bool(args.ble_characteristic):
        raise SystemExit("Both --ble-address and --ble-characteristic are required for BLE delivery.")

    line_queue: queue.Queue[str] = queue.Queue(maxsize=args.max_queue)
    stats = {"received": 0, "dropped_queue": 0, "invalid": 0}
    stop = threading.Event()

    predictor = RunningFormPredictor(args.model, args.normalizer)
    if not predictor.production_ready:
        print("WARNING: training normalizer not supplied; model alerts disabled unless --allow-demo-alerts is used.")
    delivery = BluetoothAlertDelivery(
        output_dir=args.output_dir,
        session_id=args.session_id,
        address=args.ble_address,
        characteristic=args.ble_characteristic,
        scan_timeout_s=args.ble_scan_timeout_s,
    )
    if delivery.configured:
        retry = delivery.retry_pending()
        print(f"BLE delivery configured; retried={retry['sent']} pending={retry['remaining']}")
    else:
        print("BLE not configured; alerts will be stored in local outbox.")

    session = UNOQInferenceSession(
        output_dir=args.output_dir,
        session_id=args.session_id,
        predictor=predictor,
        calibration=Calibration.load(args.calibration),
        config=IMUConfig(
            sample_rate_hz=args.sample_rate_hz,
            body_frame_rotation=tuple(args.body_frame_rotation),
            impact_band_hz=(5.0, min(20.0, args.sample_rate_hz * 0.45)),
            stride_s=2.0,
        ),
        warmup_s=args.warmup_s,
        bad_form_threshold=args.bad_form_threshold,
        cooldown_s=args.cooldown_s,
        allow_demo_alerts=args.allow_demo_alerts,
        enable_metric_alerts=args.enable_metric_alerts,
        delivery=delivery,
    )

    def ingest_batch(batch: str) -> None:
        accepted = 0
        for line in str(batch).splitlines():
            message = line.strip()
            if not message:
                continue
            try:
                line_queue.put_nowait(message)
                stats["received"] += 1
                stats["last_sensor_csv"] = message
                accepted += 1
            except queue.Full:
                stats["dropped_queue"] += 1
        if accepted and (stats["received"] <= 10 or stats["received"] % 200 == 0):
            print(
                "BRIDGE_BATCH_RECEIVED "
                f"accepted={accepted} total_received={stats['received']} "
                f"queue_size={line_queue.qsize()} dropped_queue={stats['dropped_queue']} "
                f"last_sensor_csv={stats.get('last_sensor_csv', '')}",
                flush=True,
            )

    def worker() -> None:
        print(f"Model: {args.model}", flush=True)
        print(f"Saving: {session.raw_path}, {session.features_path}, {session.predictions_path}", flush=True)
        while not stop.is_set():
            try:
                first_line = line_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            lines = [first_line]
            batch_deadline = time.monotonic() + max(0.0, args.worker_batch_wait_s)
            while len(lines) < args.worker_batch_size:
                timeout_s = max(0.0, batch_deadline - time.monotonic())
                if timeout_s == 0.0:
                    break
                try:
                    lines.append(line_queue.get(timeout=timeout_s))
                except queue.Empty:
                    break

            payload = None
            for line in lines:
                try:
                    sample = parse_imu(line)
                except ProtocolError as error:
                    stats["invalid"] += 1
                    if stats["invalid"] <= 10 or stats["invalid"] % 100 == 0:
                        print(f"dropped invalid bridge packet: {error}", flush=True)
                    continue
                try:
                    payload, _alert = session.ingest(sample)
                    stats["processed"] = stats.get("processed", 0) + 1
                except Exception as error:
                    stats["processing_errors"] = stats.get("processing_errors", 0) + 1
                    stats["last_processing_error"] = f"{type(error).__name__}: {error}"
                    print(f"MODEL_PIPELINE_ERROR {stats['last_processing_error']}", flush=True)
                    _log_bridge_status(
                        "model_error",
                        stats,
                        extra={
                            "queue_size": line_queue.qsize(),
                            "err": stats["last_processing_error"],
                        },
                    )
                    continue
                if payload:
                    payload["bridge_stats"] = dict(stats)
                    if args.mcu_ble:
                        dashboard_payload = json.loads(_model_output_mcu_ble_payload(payload))
                        payload["mcu_ble"] = _send_mcu_payload(dashboard_payload)
                    print(f"FULL_PAYLOAD_JSON={json.dumps(payload, ensure_ascii=False, default=str)}", flush=True)

            stats["last_worker_batch_size"] = len(lines)

    Bridge.provide("formsense/imu_batch", ingest_batch)
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    def poll_mcu_imu() -> None:
        now_s = time.monotonic()
        try:
            with BRIDGE_LOCK:
                batch = Bridge.call("formsense/pop_imu_batch")
        except Exception as error:
            if stats.get("poll_errors", 0) < 5:
                print(f"BRIDGE_POLL_ERROR {type(error).__name__}: {error}", flush=True)
            stats["poll_errors"] = stats.get("poll_errors", 0) + 1
            time.sleep(0.05)
            return
        if batch:
            ingest_batch(str(batch))
        if now_s - float(stats.get("last_sensor_status_s", 0.0)) >= 1.0:
            stats["last_sensor_status_s"] = now_s
            result = _log_bridge_status(
                "receiving_imu" if stats["received"] else "waiting_imu",
                stats,
                extra={
                    "queue_size": line_queue.qsize(),
                    "errn": stats.get("processing_errors", 0),
                    "bs": stats.get("last_worker_batch_size", 0),
                },
            )
            print(f"BRIDGE_SENSOR_STATUS={json.dumps(result, ensure_ascii=False)}", flush=True)
        time.sleep(0.005)

    try:
        print("RouterBridge receiver ready: formsense/imu_batch", flush=True)
        print("Waiting 2s for MCU Bridge methods to register...", flush=True)
        time.sleep(2.0)
        result = _log_bridge_status("waiting_imu", stats)
        print(f"BRIDGE_STARTUP_STATUS={json.dumps(result, ensure_ascii=False)}", flush=True)
        App.run(user_loop=poll_mcu_imu)
    finally:
        stop.set()
        session.close()


if __name__ == "__main__":
    main()
