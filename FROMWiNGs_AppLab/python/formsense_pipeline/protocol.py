"""Compact line-oriented UART messages from an Arduino Nano IMU transmitter."""

from dataclasses import dataclass
from typing import Mapping


class ProtocolError(ValueError):
    """A UART packet is malformed or fails CRC validation."""


@dataclass(frozen=True)
class ImuSample:
    seq: int
    timestamp_s: float
    acc_x_g: float
    acc_y_g: float
    acc_z_g: float
    gyro_x_dps: float
    gyro_y_dps: float
    gyro_z_dps: float


RAW_COLUMNS = [
    "seq",
    "timestamp_s",
    "acc_x_g",
    "acc_y_g",
    "acc_z_g",
    "gyro_x_dps",
    "gyro_y_dps",
    "gyro_z_dps",
]

FEATURE_KEYS = [
    "cadence_spm",
    "vertical_oscillation_cm",
    "gct_flight_balance_ms",
    "impact_loading_rate_bw_s",
    "trunk_forward_lean_deg",
    "left_right_asymmetry_pct",
    "heel_strike_likelihood",
]


def crc16_ccitt(payload: str) -> int:
    crc = 0xFFFF
    for byte in payload.encode("ascii"):
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def _wrap(payload: str) -> str:
    return f"{payload}*{crc16_ccitt(payload):04X}"


def _unwrap(line: str) -> str:
    message = line.strip()
    if "*" not in message:
        raise ProtocolError("missing CRC separator")
    payload, transmitted_crc = message.rsplit("*", 1)
    try:
        expected = int(transmitted_crc, 16)
    except ValueError as exc:
        raise ProtocolError("invalid CRC encoding") from exc
    if crc16_ccitt(payload) != expected:
        raise ProtocolError("CRC mismatch")
    return payload


def encode_imu(sample: ImuSample) -> str:
    payload = (
        f"@IMU,{sample.seq},{sample.timestamp_s:.4f},"
        f"{sample.acc_x_g:.5f},{sample.acc_y_g:.5f},{sample.acc_z_g:.5f},"
        f"{sample.gyro_x_dps:.4f},{sample.gyro_y_dps:.4f},{sample.gyro_z_dps:.4f}"
    )
    return _wrap(payload)


def parse_imu(line: str) -> ImuSample:
    message = line.strip()
    if not message:
        raise ProtocolError("empty packet")
    if "*" in message:
        payload = _unwrap(message)
        fields = payload.split(",")
        if len(fields) not in (9, 12) or fields[0] != "@IMU":
            raise ProtocolError("expected @IMU packet with accel and gyro values")
        offset = 1
    else:
        if message.lower().startswith("seq,"):
            raise ProtocolError("CSV header")
        fields = message.split(",")
        if len(fields) != 8:
            raise ProtocolError("expected CSV packet with 8 IMU fields")
        offset = 0
    try:
        return ImuSample(
            seq=int(fields[offset]),
            timestamp_s=float(fields[offset + 1]),
            acc_x_g=float(fields[offset + 2]),
            acc_y_g=float(fields[offset + 3]),
            acc_z_g=float(fields[offset + 4]),
            gyro_x_dps=float(fields[offset + 5]),
            gyro_y_dps=float(fields[offset + 6]),
            gyro_z_dps=float(fields[offset + 7]),
        )
    except ValueError as exc:
        raise ProtocolError("non-numeric IMU field") from exc


def encode_ack(seq: int) -> str:
    return _wrap(f"@ACK,{seq}")


def encode_feature(window_id: int, end_time_s: float, features: Mapping[str, float], label: str) -> str:
    values = ",".join(f"{features[key]:.3f}" for key in FEATURE_KEYS)
    safe_label = label.replace(",", "_")[:16]
    return _wrap(f"@FEAT,{window_id},{end_time_s:.3f},{values},{safe_label}")


def encode_alert(severity: str, end_time_s: float, message: str) -> str:
    safe_message = message.replace(",", ";").replace("\n", " ")[:96]
    safe_message = safe_message.encode("ascii", errors="ignore").decode("ascii").strip() or "Bad form detected"
    return _wrap(f"@ALERT,{severity},{end_time_s:.3f},{safe_message}")
