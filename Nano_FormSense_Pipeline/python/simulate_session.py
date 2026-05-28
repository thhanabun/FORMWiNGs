#!/usr/bin/env python3
"""Generate a labeled synthetic run and process it without Nano hardware."""

import argparse
import csv
import math
from pathlib import Path

from formsense_pipeline.feedback import FeedbackEngine
from formsense_pipeline.pipeline import RunningFormPipeline
from formsense_pipeline.protocol import ImuSample, RAW_COLUMNS


def samples(duration_s: float, sample_rate_hz: int, cadence_spm: float, bad_form: bool):
    step_hz = cadence_spm / 60.0
    lean_deg = 22.0 if bad_form else 8.0
    lean = math.radians(lean_deg)
    for seq in range(int(duration_s * sample_rate_hz)):
        timestamp = seq / sample_rate_hz
        phase = (timestamp * step_hz) % 1.0
        step_side = int(timestamp * step_hz) % 2
        asymmetry = 1.28 if bad_form and step_side == 0 else 1.0
        impact = math.exp(-((phase - 0.06) / 0.045) ** 2) * (0.30 if bad_form else 0.16) * asymmetry
        motion = math.sin(2 * math.pi * step_hz * timestamp)
        lateral = (0.09 if bad_form else 0.03) * math.sin(math.pi * step_hz * timestamp)
        yield ImuSample(
            seq=seq,
            timestamp_s=timestamp,
            acc_x_g=-math.sin(lean) + lateral,
            acc_y_g=lateral,
            acc_z_g=math.cos(lean) + impact + 0.035 * motion,
            gyro_x_dps=2.1 * motion,
            gyro_y_dps=2.8 * motion,
            gyro_z_dps=(16.0 if bad_form else 5.0) * motion,
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("data/synthetic"))
    parser.add_argument("--session-id", default="bad_form_demo")
    parser.add_argument("--duration-s", type=float, default=15.0)
    parser.add_argument("--sample-rate-hz", type=int, default=200)
    parser.add_argument("--cadence-spm", type=float, default=168.0)
    parser.add_argument("--good-form", action="store_true")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--alert-mode", choices=["off", "realtime", "five_min", "both"], default="off")
    parser.add_argument("--summary-interval-s", type=float, default=300.0)
    args = parser.parse_args()
    if args.alert_mode != "off" and args.baseline is None:
        parser.error("--baseline is required when --alert-mode is enabled")
    label = "GOOD_FORM" if args.good_form else "BAD_FORM"
    pipeline = RunningFormPipeline(args.output_dir, args.session_id, label)
    feedback = (
        FeedbackEngine.load(args.baseline, mode=args.alert_mode, summary_interval_s=args.summary_interval_s)
        if args.alert_mode != "off"
        else None
    )
    sender_raw_path = args.output_dir / f"{args.session_id}_sender_input.csv"
    alert_path = args.output_dir / f"{args.session_id}_alerts.csv"
    args.output_dir.mkdir(parents=True, exist_ok=True)
    emitted = 0
    alert_count = 0
    with sender_raw_path.open("w", newline="", encoding="utf-8") as handle, alert_path.open(
        "w", newline="", encoding="utf-8"
    ) as alert_handle:
        writer = csv.DictWriter(handle, fieldnames=RAW_COLUMNS)
        writer.writeheader()
        alert_writer = csv.DictWriter(alert_handle, fieldnames=["timestamp_s", "event_type", "severity", "message"])
        alert_writer.writeheader()
        for sample in samples(args.duration_s, args.sample_rate_hz, args.cadence_spm, not args.good_form):
            writer.writerow(sample.__dict__)
            features = pipeline.ingest(sample)
            if features:
                emitted += 1
                if feedback:
                    for event in feedback.ingest(sample.timestamp_s, features):
                        alert_writer.writerow(event.__dict__)
                        print(f"{event.event_type} {event.severity}: {event.message}")
                        alert_count += 1
    pipeline.close()
    print(f"synthetic label={label} windows={emitted}")
    print(f"sender input: {sender_raw_path}")
    print(f"features: {pipeline.recorder.feature_path}")
    if feedback:
        print(f"alerts: {alert_path} events={alert_count}")


if __name__ == "__main__":
    main()
