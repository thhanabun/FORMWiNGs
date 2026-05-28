"""Modulino Thermo integration and rule-based heat recommendations."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ThermalReading:
    mcu_ms: int
    temperature_c: float
    humidity_pct: float
    received_monotonic_s: float


def _heat_index_c(temperature_c: float, humidity_pct: float) -> tuple[float, str]:
    """Return heat-index proxy in Celsius plus method name."""

    if temperature_c < 26.7:
        return temperature_c, "ambient_temperature_proxy"

    temp_f = temperature_c * 9.0 / 5.0 + 32.0
    rh = humidity_pct
    hi_f = (
        -42.379
        + 2.04901523 * temp_f
        + 10.14333127 * rh
        - 0.22475541 * temp_f * rh
        - 0.00683783 * temp_f**2
        - 0.05481717 * rh**2
        + 0.00122874 * temp_f**2 * rh
        + 0.00085282 * temp_f * rh**2
        - 0.00000199 * temp_f**2 * rh**2
    )

    if rh < 13.0 and 80.0 <= temp_f <= 112.0:
        hi_f -= ((13.0 - rh) / 4.0) * math.sqrt((17.0 - abs(temp_f - 95.0)) / 17.0)
    elif rh > 85.0 and 80.0 <= temp_f <= 87.0:
        hi_f += ((rh - 85.0) / 10.0) * ((87.0 - temp_f) / 5.0)

    heat_index_c = (hi_f - 32.0) * 5.0 / 9.0
    if rh >= 40.0 and heat_index_c < temperature_c:
        heat_index_c = temperature_c
    return heat_index_c, "NWS_Rothfusz_regression"


def _recommendation(heat_index_c: float, temperature_c: float, humidity_pct: float) -> dict[str, Any]:
    if heat_index_c >= 40.0:
        return {
            "severity": "ALERT",
            "code": "heat_danger",
            "message_th": "ความร้อนสะสมสูงมาก ลดความเข้ม ดื่มน้ำ และพิจารณาหยุดพักในที่เย็น",
            "device_message": "Heat risk high; slow down, hydrate, and consider stopping.",
        }
    if heat_index_c >= 35.0:
        return {
            "severity": "WARN",
            "code": "heat_warning",
            "message_th": "อากาศร้อนชื้นระดับเสี่ยง ลด pace และเลี่ยง interval หนัก",
            "device_message": "Heat risk elevated; reduce pace and avoid hard intervals.",
        }
    if heat_index_c >= 30.0 or (temperature_c >= 30.0 and humidity_pct >= 70.0):
        return {
            "severity": "INFO",
            "code": "heat_caution",
            "message_th": "อากาศเริ่มร้อนชื้น คุม pace และจิบน้ำเป็นระยะ",
            "device_message": "Warm humid conditions; control pace and hydrate.",
        }
    return {
        "severity": "OK",
        "code": "thermal_ok",
        "message_th": "สภาพอากาศยังอยู่ในช่วงปกติสำหรับการวิ่ง",
        "device_message": "Thermal conditions look normal.",
    }


def _risk_state(heat_index_c: float) -> str:
    if heat_index_c >= 40.0:
        return "Danger"
    if heat_index_c >= 35.0:
        return "Warning"
    if heat_index_c >= 30.0:
        return "Caution"
    return "Normal"


def _risk_score(heat_index_c: float) -> int:
    if heat_index_c < 30.0:
        score = 5.0 + (max(heat_index_c, 0.0) / 30.0) * 15.0
    elif heat_index_c < 35.0:
        score = 30.0 + ((heat_index_c - 30.0) / 5.0) * 20.0
    elif heat_index_c < 40.0:
        score = 55.0 + ((heat_index_c - 35.0) / 5.0) * 25.0
    else:
        score = 90.0 + min((heat_index_c - 40.0) / 15.0, 1.0) * 10.0
    return int(round(max(0.0, min(100.0, score))))


def parse_thermal_csv(payload: str, now_s: float) -> ThermalReading | None:
    """Parse MCU thermal CSV: mcu_ms,temperature_c,humidity_pct."""

    text = payload.strip()
    if not text:
        return None
    parts = text.split(",")
    if len(parts) != 3:
        raise ValueError("thermal payload must be mcu_ms,temperature_c,humidity_pct")

    mcu_ms = int(parts[0])
    temperature_c = float(parts[1])
    humidity_pct = float(parts[2])
    if not math.isfinite(temperature_c) or not math.isfinite(humidity_pct):
        raise ValueError("thermal payload contains non-finite values")
    if temperature_c < 0.0 or temperature_c > 50.0:
        raise ValueError("temperature outside expected running-environment range")
    if humidity_pct < 0.0 or humidity_pct > 100.0:
        raise ValueError("humidity must be between 0 and 100 percent")
    return ThermalReading(mcu_ms, temperature_c, humidity_pct, now_s)


class ThermalMonitor:
    """Keeps the latest Modulino Thermo reading and formats prediction payload output."""

    def __init__(self, stale_after_s: float = 10.0) -> None:
        self.stale_after_s = stale_after_s
        self.latest: ThermalReading | None = None
        self.invalid = 0
        self.last_error: str | None = None

    def update_from_bridge(self, payload: str, now_s: float) -> ThermalReading | None:
        reading = parse_thermal_csv(payload, now_s)
        if reading is None:
            return None
        self.latest = reading
        self.last_error = None
        return reading

    def mark_invalid(self, error: Exception) -> None:
        self.invalid += 1
        self.last_error = f"{type(error).__name__}: {error}"

    def output(self, now_s: float) -> dict[str, Any]:
        if self.latest is None:
            return {
                "environment": {"status": "unavailable"},
                "recommendation": {
                    "severity": "INFO",
                    "code": "thermal_unavailable",
                    "message_th": "ยังไม่มีข้อมูลอุณหภูมิ/ความชื้นจาก Modulino Thermal",
                    "device_message": "Thermal sensor unavailable.",
                },
            }

        age_s = max(0.0, now_s - self.latest.received_monotonic_s)
        heat_index, method = _heat_index_c(self.latest.temperature_c, self.latest.humidity_pct)
        recommendation = _recommendation(heat_index, self.latest.temperature_c, self.latest.humidity_pct)
        status = "stale" if age_s > self.stale_after_s else "ok"
        if status == "stale":
            recommendation = {
                "severity": "INFO",
                "code": "thermal_stale",
                "message_th": "ข้อมูลอุณหภูมิ/ความชื้นเก่าเกินไป ใช้เป็นบริบทชั่วคราว",
                "device_message": "Thermal reading is stale.",
            }

        return {
            "environment": {
                "status": status,
                "temperature_c": round(self.latest.temperature_c, 2),
                "humidity_pct": round(self.latest.humidity_pct, 2),
                "heat_index_c": round(heat_index, 2),
                "risk_state": _risk_state(heat_index),
                "risk_score": _risk_score(heat_index),
                "method": method,
                "age_s": round(age_s, 2),
            },
            "recommendation": recommendation,
        }
