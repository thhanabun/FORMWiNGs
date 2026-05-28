"""Waist-mounted accelerometer/gyroscope calibration and streaming filters."""

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

from .protocol import ImuSample


@dataclass
class Calibration:
    gyro_bias_dps: tuple[float, float, float] = (0.0, 0.0, 0.0)
    neutral_pitch_deg: float = 0.0

    @classmethod
    def load(cls, path: Path | None) -> "Calibration":
        if path is None or not path.exists():
            return cls()
        values = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            gyro_bias_dps=tuple(values.get("gyro_bias_dps", (0.0, 0.0, 0.0))),
            neutral_pitch_deg=float(values.get("neutral_pitch_deg", 0.0)),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


class LowPass:
    def __init__(self, cutoff_hz: float):
        self.cutoff_hz = cutoff_hz
        self.value: float | None = None

    def update(self, value: float, dt: float) -> float:
        if self.value is None:
            self.value = value
            return value
        rc = 1.0 / (2.0 * math.pi * self.cutoff_hz)
        alpha = dt / (rc + dt)
        self.value += alpha * (value - self.value)
        return self.value


class HighPass:
    def __init__(self, cutoff_hz: float):
        self.cutoff_hz = cutoff_hz
        self.value = 0.0
        self.previous_input: float | None = None

    def update(self, value: float, dt: float) -> float:
        if self.previous_input is None:
            self.previous_input = value
            return 0.0
        rc = 1.0 / (2.0 * math.pi * self.cutoff_hz)
        alpha = rc / (rc + dt)
        self.value = alpha * (self.value + value - self.previous_input)
        self.previous_input = value
        return self.value


class OrientationFusion:
    """Complementary roll/pitch estimate; yaw is not observable without a reference."""

    def __init__(self) -> None:
        self.pitch = 0.0
        self.roll = 0.0
        self.initialized = False

    def update(
        self,
        acc: tuple[float, float, float],
        gyro: tuple[float, float, float],
        dt: float,
    ) -> tuple[float, float]:
        ax, ay, az = acc
        gx, gy, _ = gyro
        pitch_acc = math.degrees(math.atan2(-ax, math.sqrt(ay * ay + az * az)))
        roll_acc = math.degrees(math.atan2(ay, az))
        if not self.initialized:
            self.pitch, self.roll = pitch_acc, roll_acc
            self.initialized = True
            return self.roll, self.pitch
        self.pitch = 0.98 * (self.pitch + gy * dt) + 0.02 * pitch_acc
        self.roll = 0.98 * (self.roll + gx * dt) + 0.02 * roll_acc
        return self.roll, self.pitch


class SensorFilter:
    """Produces channels for posture and impact without hiding useful impact peaks."""

    def __init__(self, calibration: Calibration):
        self.calibration = calibration
        self.previous_time: float | None = None
        self.acc_posture = [LowPass(5.0) for _ in range(3)]
        self.acc_impact = [LowPass(35.0) for _ in range(3)]
        self.dynamic_vertical = HighPass(0.5)
        self.gyro = [LowPass(20.0) for _ in range(3)]
        self.orientation = OrientationFusion()

    def process(self, sample: ImuSample) -> dict[str, float]:
        dt = sample.timestamp_s - self.previous_time if self.previous_time is not None else 0.01
        dt = max(0.002, min(dt, 0.1))
        self.previous_time = sample.timestamp_s
        raw_acc = (sample.acc_x_g, sample.acc_y_g, sample.acc_z_g)
        raw_gyro = (sample.gyro_x_dps, sample.gyro_y_dps, sample.gyro_z_dps)
        gyro = tuple(raw_gyro[i] - self.calibration.gyro_bias_dps[i] for i in range(3))
        acc_posture = tuple(self.acc_posture[i].update(raw_acc[i], dt) for i in range(3))
        acc_impact = tuple(self.acc_impact[i].update(raw_acc[i], dt) for i in range(3))
        gyro_filtered = tuple(self.gyro[i].update(gyro[i], dt) for i in range(3))
        roll, pitch = self.orientation.update(acc_posture, gyro_filtered, dt)
        vertical_dynamic = self.dynamic_vertical.update(acc_impact[2], dt)
        return {
            "timestamp_s": sample.timestamp_s,
            "dt_s": dt,
            "acc_x_filtered_g": acc_impact[0],
            "acc_y_filtered_g": acc_impact[1],
            "acc_z_filtered_g": acc_impact[2],
            "vertical_dynamic_g": vertical_dynamic,
            "gyro_x_cal_dps": gyro_filtered[0],
            "gyro_y_cal_dps": gyro_filtered[1],
            "gyro_z_cal_dps": gyro_filtered[2],
            "roll_deg": roll,
            "pitch_deg": pitch - self.calibration.neutral_pitch_deg,
        }
