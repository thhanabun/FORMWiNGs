"""Collect Arduino Nano IMU UART packets into labeled local training CSV files."""

import argparse
import csv
from datetime import datetime, timezone
from pathlib import Path

import serial

from formsense_pipeline.filters import Calibration
from formsense_pipeline.feedback import FeedbackEngine
from formsense_pipeline.pipeline import RunningFormPipeline
from formsense_pipeline.protocol import ProtocolError, encode_ack, encode_alert, encode_feature, parse_imu


def send_line(uart: serial.Serial, message: str) -> None:
    uart.write((message + "\n").encode("ascii"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Arduino Nano FormSense IMU collector")
    parser.add_argument("--port", required=True, help="Nano serial port, for example /dev/cu.usbmodem1101.")
    parser.add_argument("--baud", type=int, default=460800)
    parser.add_argument("--output-dir", type=Path, default=Path("data/sessions"))
    parser.add_argument("--session-id", default=datetime.now(timezone.utc).strftime("run_%Y%m%dT%H%M%SZ"))
    parser.add_argument("--label", choices=["GOOD_FORM", "BAD_FORM", "UNLABELED"], default="UNLABELED")
    parser.add_argument("--calibration", type=Path)
    parser.add_argument("--feedback", action="store_true", help="Send ACK and @FEAT messages back to a listening peer.")
    parser.add_argument("--baseline", type=Path, help="Personal GOOD_FORM baseline JSON for coaching alerts.")
    parser.add_argument("--alert-mode", choices=["off", "realtime", "five_min", "both"], default="off")
    parser.add_argument("--realtime-cooldown-s", type=float, default=20.0)
    parser.add_argument("--summary-interval-s", type=float, default=300.0)
    args = parser.parse_args()
    if args.alert_mode != "off" and args.baseline is None:
        parser.error("--baseline is required when --alert-mode is enabled")
    print(f"Nano UART={args.port}@{args.baud} session={args.session_id} label={args.label}")
    with serial.Serial(args.port, args.baud, timeout=0.1) as uart:
        pipeline = RunningFormPipeline(
            output_dir=args.output_dir,
            session_id=args.session_id,
            form_label=args.label,
            calibration=Calibration.load(args.calibration),
        )
        feedback = (
            FeedbackEngine.load(
                args.baseline,
                mode=args.alert_mode,
                realtime_cooldown_s=args.realtime_cooldown_s,
                summary_interval_s=args.summary_interval_s,
            )
            if args.alert_mode != "off"
            else None
        )
        alert_handle = None
        alert_writer = None
        if feedback:
            alert_path = args.output_dir / f"{args.session_id}_alerts.csv"
            alert_handle = alert_path.open("w", newline="", encoding="utf-8", buffering=1)
            alert_writer = csv.DictWriter(alert_handle, fieldnames=["timestamp_s", "event_type", "severity", "message"])
            alert_writer.writeheader()
            print(f"Alerts enabled ({args.alert_mode}); saving to {alert_path}")
        print(f"Saving raw/filtered/features CSV under {args.output_dir.resolve()}")
        previous_seq: int | None = None
        try:
            while True:
                line = uart.readline().decode("ascii", errors="ignore").strip()
                if not line:
                    continue
                try:
                    sample = parse_imu(line)
                    expected_seq = (previous_seq + 1) & 0xFFFFFFFF if previous_seq is not None else sample.seq
                    if sample.seq != expected_seq:
                        print(f"WARNING packet gap: expected seq={expected_seq}, received seq={sample.seq}")
                    previous_seq = sample.seq
                    features = pipeline.ingest(sample)
                    if args.feedback:
                        send_line(uart, encode_ack(sample.seq))
                    if features is not None:
                        print(f"window={pipeline.window_id} features={features}")
                        if args.feedback:
                            send_line(uart, encode_feature(pipeline.window_id, sample.timestamp_s, features, args.label))
                        if feedback:
                            for event in feedback.ingest(sample.timestamp_s, features):
                                print(f"{event.event_type} {event.severity}: {event.message}")
                                alert_writer.writerow(event.__dict__)
                                if args.feedback:
                                    send_line(uart, encode_alert(event.severity, event.timestamp_s, event.message))
                except ProtocolError as error:
                    print(f"dropped invalid UART packet: {error}")
        except KeyboardInterrupt:
            print(f"\nstopped; features saved to {pipeline.recorder.feature_path}")
        finally:
            pipeline.close()
            if alert_handle:
                alert_handle.close()


if __name__ == "__main__":
    main()
