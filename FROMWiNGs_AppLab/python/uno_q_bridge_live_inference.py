#!/usr/bin/env python3
"""Run FormSense inference from UNO Q MCU RouterBridge batches."""

from __future__ import annotations

import argparse
import json
import queue
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from arduino.app_utils import App, Bridge

from formsense_pipeline.filters import Calibration
from formsense_pipeline.protocol import RAW_COLUMNS, ProtocolError, parse_imu
from formsense_pipeline.thermal import ThermalMonitor
from formsense_pipeline.unoq_model import IMUConfig, RunningFormPredictor
from uno_q_live_inference import DEFAULT_MODEL, UNOQInferenceSession


MCU_BLE_FRAME_LIMIT = 64
MCU_BLE_CHUNK_DATA_SIZE = 64
MCU_BLE_NOTIFY_INTERVAL_S = 0.04
MCU_BLE_PAYLOAD_GAP_S = 0.05
BLE_STABLE_BEFORE_SEND_S = 3.0
MIN_PREDICTION_SEND_INTERVAL_S = 0.75

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
    parser.add_argument("--enable-metric-alerts", action="store_true")
    parser.add_argument("--worker-batch-size", type=int, default=80)
    parser.add_argument("--worker-batch-wait-s", type=float, default=0.05)
    parser.add_argument("--max-queue", type=int, default=30000)
    return parser


DOMINANT_FEATURE_CODES = {
    "cadence_spm": "c",
    "vertical_oscillation_cm": "vo",
    "gct_flight_balance_ms": "gb",
    "impact_loading_rate_bw_s": "lr",
    "trunk_forward_lean_deg": "ln",
    "left_right_asymmetry_pct": "la",
    "heel_strike_likelihood": "hs",
}

RISK_STATE_CODES = {
    "Normal": "n",
    "Caution": "c",
    "Warning": "w",
    "Danger": "d",
}

ENV_STATUS_CODES = {
    "ok": "ok",
    "unavailable": "u",
    "stale": "s",
}


def _compact_number(value: object, digits: int) -> float | int | None:
    try:
        rounded = round(float(value), digits)
    except (TypeError, ValueError):
        return None
    if rounded == int(rounded):
        return int(rounded)
    return rounded


def _put_number(output: dict[str, object], key: str, value: object, digits: int) -> None:
    compact = _compact_number(value, digits)
    if compact is not None:
        output[key] = compact


def _compact_live_payload(payload: dict[str, object]) -> dict[str, object]:
    """Return the smallest BLE live packet; local logs still keep the full schema."""

    features = payload.get("features")
    if not isinstance(features, dict):
        features = {}
    diagnostics = payload.get("diagnostics")
    if not isinstance(diagnostics, dict):
        diagnostics = {}
    probabilities = payload.get("probabilities")
    if not isinstance(probabilities, dict):
        probabilities = {}

    output: dict[str, object] = {
        "t": "p",
        "f": 0 if payload.get("class") == "Good" else 1,
    }
    if payload.get("window_id") is not None:
        output["w"] = payload.get("window_id")

    _put_number(output, "ts", payload.get("timestamp_s"), 2)
    _put_number(output, "c", features.get("cadence_spm"), 1)
    _put_number(output, "vo", features.get("vertical_oscillation_cm"), 2)
    _put_number(output, "gb", features.get("gct_flight_balance_ms"), 1)
    _put_number(output, "lr", features.get("impact_loading_rate_bw_s"), 2)
    _put_number(output, "ln", features.get("trunk_forward_lean_deg"), 1)
    _put_number(output, "la", features.get("left_right_asymmetry_pct"), 1)
    _put_number(output, "hs", features.get("heel_strike_likelihood"), 2)
    _put_number(output, "g", diagnostics.get("gct_ms"), 1)
    _put_number(output, "ft", diagnostics.get("flight_time_ms"), 1)
    _put_number(output, "pf", diagnostics.get("peak_vgrf_bw_estimate"), 2)
    _put_number(output, "tp", diagnostics.get("footstrike_time_to_peak_ms"), 1)
    _put_number(output, "fb", diagnostics.get("fallback_window"), 0)
    _put_number(output, "ds", diagnostics.get("detected_step_events"), 0)
    _put_number(output, "pg", probabilities.get("Good"), 3)
    _put_number(output, "pb", probabilities.get("Bad Form"), 3)

    dominant_feature = payload.get("dominant_feature")
    if dominant_feature in DOMINANT_FEATURE_CODES:
        output["df"] = DOMINANT_FEATURE_CODES[dominant_feature]

    priority_trigger = payload.get("priority_trigger")
    if isinstance(priority_trigger, dict):
        severity = priority_trigger.get("severity")
        code = priority_trigger.get("code")
        if severity:
            output["ps"] = str(severity).lower()
        if code:
            output["pc"] = code
        _put_number(output, "pv", priority_trigger.get("value"), 2)
        _put_number(output, "px", priority_trigger.get("threshold"), 2)

    environment = payload.get("environment")
    if isinstance(environment, dict) and environment.get("status") != "unavailable":
        output["st"] = ENV_STATUS_CODES.get(str(environment.get("status")), str(environment.get("status", ""))[:1])
        _put_number(output, "et", environment.get("temperature_c"), 1)
        _put_number(output, "eh", environment.get("humidity_pct"), 0)
        _put_number(output, "ei", environment.get("heat_index_c"), 1)
        risk_state = environment.get("risk_state")
        if risk_state:
            output["er"] = RISK_STATE_CODES.get(str(risk_state), str(risk_state)[:1].lower())
        _put_number(output, "es", environment.get("risk_score"), 0)
        _put_number(output, "ea", environment.get("age_s"), 1)

    recommendation = payload.get("recommendation")
    if isinstance(recommendation, dict):
        severity = recommendation.get("severity")
        code = recommendation.get("code")
        if severity:
            output["rs"] = str(severity).lower()
        if code:
            output["rc"] = code

    return output


def _send_mcu_payload(payload: dict[str, object]) -> dict[str, object]:
    """Send the full dashboard payload as small BLE notification chunks."""

    payload = _compact_live_payload(payload)
    compact = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), default=str)
    chunks = [
        compact[index : index + MCU_BLE_CHUNK_DATA_SIZE]
        for index in range(0, len(compact), MCU_BLE_CHUNK_DATA_SIZE)
    ]
    print(
        "MCU_BLE_PAYLOAD_SUMMARY="
        f"type={payload.get('type', '')} bytes={len(compact)} chunks={len(chunks)}",
        flush=True,
    )
    try:
        with BRIDGE_LOCK:
            for index, chunk in enumerate(chunks):
                if len(chunk) > MCU_BLE_FRAME_LIMIT:
                    return {
                        "status": "ERROR",
                        "reason": "ble_frame_too_large",
                        "frame_bytes": len(chunk),
                    }
                Bridge.call("formsense/ble_begin", "1")
                Bridge.call("formsense/ble_chunk", chunk)
                committed = Bridge.call("formsense/ble_commit", "1")
                if not committed:
                    return {
                        "status": "ERROR",
                        "reason": "mcu_ble_queue_full",
                        "bytes": len(compact),
                        "chunks": len(chunks),
                        "failed_index": index,
                    }
                time.sleep(0.005)
            time.sleep((len(chunks) * MCU_BLE_NOTIFY_INTERVAL_S) + MCU_BLE_PAYLOAD_GAP_S)
        return {"status": "SENT_TO_MCU_TEXT", "bytes": len(compact), "chunks": len(chunks)}
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


def _parsed_sample_ok(sample: object, stats: dict[str, object]) -> bool:
    try:
        seq = int(getattr(sample, "seq"))
        timestamp_s = float(getattr(sample, "timestamp_s"))
        acc_values = [
            float(getattr(sample, "acc_x_g")),
            float(getattr(sample, "acc_y_g")),
            float(getattr(sample, "acc_z_g")),
        ]
        gyro_values = [
            float(getattr(sample, "gyro_x_dps")),
            float(getattr(sample, "gyro_y_dps")),
            float(getattr(sample, "gyro_z_dps")),
        ]
    except (TypeError, ValueError):
        stats["invalid_range"] = stats.get("invalid_range", 0) + 1
        return False

    if seq < 0 or seq > 10_000_000:
        stats["invalid_range"] = stats.get("invalid_range", 0) + 1
        return False
    if timestamp_s < 0.0 or timestamp_s > 1_000_000.0:
        stats["invalid_range"] = stats.get("invalid_range", 0) + 1
        return False
    if any(abs(value) > 16.0 for value in acc_values):
        stats["invalid_range"] = stats.get("invalid_range", 0) + 1
        return False
    if any(abs(value) > 3000.0 for value in gyro_values):
        stats["invalid_range"] = stats.get("invalid_range", 0) + 1
        return False

    stats["last_valid_seq"] = seq
    stats["last_valid_timestamp_s"] = timestamp_s
    return True


def main() -> None:
    args = _parser().parse_args()
    if not args.model.exists():
        raise SystemExit(f"Model file not found: {args.model}")

    line_queue: queue.Queue[str] = queue.Queue(maxsize=args.max_queue)
    stats = {"received": 0, "dropped_queue": 0, "invalid": 0, "ble_connected": False}
    latest_payload_lock = threading.Lock()
    latest_payload: dict[str, object] | None = None
    latest_payload_event = threading.Event()
    stop = threading.Event()
    thermal = ThermalMonitor()
    thermal_bridge_enabled = True

    predictor = RunningFormPredictor(args.model, args.normalizer)
    if not predictor.production_ready:
        print("WARNING: training normalizer not supplied; model alerts disabled.")

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
        allow_demo_alerts=False,
        enable_metric_alerts=args.enable_metric_alerts,
    )
    local_cache_path = args.output_dir / f"{args.session_id}_local_imu_cache.csv"
    local_cache_handle = local_cache_path.open("w", encoding="utf-8", buffering=1)
    local_cache_handle.write(",".join(RAW_COLUMNS) + "\n")

    def request_shutdown(signum: int, _frame: object) -> None:
        print(f"SHUTDOWN_SIGNAL signal={signum}; closing logs", flush=True)
        stop.set()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)

    def ingest_batch(batch: str) -> None:
        accepted = 0
        for line in str(batch).splitlines():
            message = line.strip()
            if not message:
                continue
            if not stats.get("ble_connected", False):
                local_cache_handle.write(message + "\n")
                stats["cached_local"] = stats.get("cached_local", 0) + 1
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
        nonlocal latest_payload
        print(f"Model: {args.model}", flush=True)
        print(f"Saving: {session.raw_path}, {session.features_path}, {session.predictions_path}", flush=True)
        while not stop.is_set():
            if not stats.get("ble_connected", False):
                time.sleep(0.1)
                continue
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
                if not _parsed_sample_ok(sample, stats):
                    stats["invalid"] += 1
                    if stats["invalid"] <= 10 or stats["invalid"] % 100 == 0:
                        print(
                            "dropped invalid bridge packet: unreasonable IMU sample "
                            f"range={stats.get('invalid_range', 0)}",
                            flush=True,
                        )
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
                    if not stats.get("ble_connected", False):
                        continue
                    payload.update(thermal.output(time.monotonic()))
                    payload["bridge_stats"] = dict(stats)
                    with latest_payload_lock:
                        latest_payload = dict(payload)
                    latest_payload_event.set()
                    environment = payload.get("environment")
                    if not isinstance(environment, dict):
                        environment = {}
                    print(
                        "PREDICTION_SUMMARY="
                        f"window_id={payload.get('window_id', '')} "
                        f"class={payload.get('class', '')} "
                        f"dominant={payload.get('dominant_feature', '')} "
                        f"thermal={environment.get('risk_state', 'na')} "
                        f"processed={stats.get('processed', 0)} "
                        f"queue={line_queue.qsize()}",
                        flush=True,
                    )

            stats["last_worker_batch_size"] = len(lines)

    def ble_sender() -> None:
        nonlocal latest_payload
        last_send_s = 0.0
        while not stop.is_set():
            latest_payload_event.wait(timeout=0.2)
            if stop.is_set():
                return
            if not latest_payload_event.is_set():
                continue
            now_s = time.monotonic()
            connected_since = stats.get("ble_connected_since_s")
            if (
                not stats.get("ble_connected", False)
                or connected_since is None
                or now_s - float(connected_since) < BLE_STABLE_BEFORE_SEND_S
            ):
                time.sleep(0.1)
                continue
            if now_s - last_send_s < MIN_PREDICTION_SEND_INTERVAL_S:
                time.sleep(0.1)
                continue
            with latest_payload_lock:
                payload_to_send = latest_payload
                latest_payload = None
                latest_payload_event.clear()
            if payload_to_send is None:
                continue
            payload_to_send["bridge_stats"] = {
                "rx": stats.get("received", 0),
                "ok": stats.get("processed", 0),
                "bad": stats.get("invalid", 0),
                "q": line_queue.qsize(),
            }
            result = _send_mcu_payload(payload_to_send)
            last_send_s = time.monotonic()
            stats["last_mcu_ble"] = result
            print(
                "BLE_SEND_RESULT="
                f"status={result.get('status')} chunks={result.get('chunks', 0)} "
                f"bytes={result.get('bytes', 0)} queue={line_queue.qsize()}",
                flush=True,
            )

    Bridge.provide("formsense/imu_batch", ingest_batch)
    thread = threading.Thread(target=worker, daemon=True)
    sender_thread = threading.Thread(target=ble_sender, daemon=True)
    thread.start()
    sender_thread.start()

    def poll_mcu_imu() -> None:
        nonlocal thermal_bridge_enabled
        now_s = time.monotonic()
        try:
            with BRIDGE_LOCK:
                ble_connected = str(Bridge.call("formsense/ble_connected")).strip() == "1"
        except Exception as error:
            if stats.get("ble_state_errors", 0) < 5:
                print(f"BLE_STATE_ERROR {type(error).__name__}: {error}", flush=True)
            stats["ble_state_errors"] = stats.get("ble_state_errors", 0) + 1
            time.sleep(0.2)
            return

        previous_ble_connected = bool(stats.get("ble_connected", False))
        stats["ble_connected"] = ble_connected
        if previous_ble_connected != ble_connected:
            if ble_connected:
                stats["ble_connects"] = stats.get("ble_connects", 0) + 1
                stats["ble_connected_since_s"] = now_s
                stats["ble_disconnected_since_s"] = None
                print(f"BLE_CONNECTED: processing cached queue={line_queue.qsize()}", flush=True)
            else:
                stats["ble_disconnects"] = stats.get("ble_disconnects", 0) + 1
                stats["ble_connected_since_s"] = None
                stats["ble_disconnected_since_s"] = now_s
                latest_payload_event.clear()
                print("BLE_DISCONNECTED: caching local sensor data", flush=True)
        elif ble_connected and stats.get("ble_connected_since_s") is None:
            stats["ble_connected_since_s"] = now_s

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
        if thermal_bridge_enabled and now_s - float(stats.get("last_thermal_poll_s", 0.0)) >= 2.0:
            stats["last_thermal_poll_s"] = now_s
            try:
                with BRIDGE_LOCK:
                    thermal_payload = Bridge.call("formsense/pop_thermal")
                if thermal_payload:
                    reading = thermal.update_from_bridge(str(thermal_payload), now_s)
                    if reading is not None:
                        stats["thermal_ok"] = stats.get("thermal_ok", 0) + 1
            except Exception as error:
                thermal.mark_invalid(error)
                stats["thermal_errors"] = stats.get("thermal_errors", 0) + 1
                if "not available" in str(error):
                    thermal_bridge_enabled = False
                if stats["thermal_errors"] <= 3:
                    print(f"THERMAL_POLL_ERROR {type(error).__name__}: {error}", flush=True)
        if now_s - float(stats.get("last_sensor_status_s", 0.0)) >= 1.0:
            stats["last_sensor_status_s"] = now_s
            status = "receiving_imu" if stats["received"] else "waiting_imu"
            if not ble_connected:
                status = "collecting_local" if stats["received"] else "waiting_imu_local"
            result = _log_bridge_status(
                status,
                stats,
                extra={
                    "queue_size": line_queue.qsize(),
                    "errn": stats.get("processing_errors", 0),
                    "bs": stats.get("last_worker_batch_size", 0),
                    "th": stats.get("thermal_ok", 0),
                    "cache": stats.get("cached_local", 0),
                },
            )
            print(f"BRIDGE_SENSOR_STATUS={json.dumps(result, ensure_ascii=False)}", flush=True)
        time.sleep(0.02)

    try:
        print("RouterBridge receiver ready: formsense/imu_batch", flush=True)
        print("Waiting 2s for MCU Bridge methods to register...", flush=True)
        time.sleep(2.0)
        result = _log_bridge_status("waiting_imu", stats)
        print(f"BRIDGE_STARTUP_STATUS={json.dumps(result, ensure_ascii=False)}", flush=True)
        App.run(user_loop=poll_mcu_imu)
    finally:
        stop.set()
        latest_payload_event.set()
        thread.join(timeout=1.0)
        sender_thread.join(timeout=1.0)
        local_cache_handle.close()
        session.close()
        print(
            f"Logs closed: {session.raw_path}, {session.features_path}, "
            f"{session.predictions_path}, {local_cache_path}",
            flush=True,
        )


if __name__ == "__main__":
    main()
