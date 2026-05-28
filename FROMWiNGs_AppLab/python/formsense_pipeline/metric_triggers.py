"""Rule-based coaching messages for interpretable running-form metrics."""

from dataclasses import asdict, dataclass
from typing import Mapping


CADENCE_LOW_SPM = 160.0
CADENCE_HIGH_SPM = 200.0
VERTICAL_OSCILLATION_LOW_CM = 4.0
VERTICAL_OSCILLATION_HIGH_CM = 10.0
GCT_HIGH_MS = 300.0
VGRF_WARNING_BW = 2.0
VGRF_DANGER_BW = 2.5
LEAN_LOW_DEG = 3.0
LEAN_HIGH_DEG = 15.0
ASYMMETRY_HIGH_PCT = 10.0
HEEL_STRIKE_RISE_TIME_MS = 15.0
ARM_SWING_PROXY_LOW = 0.25


@dataclass(frozen=True)
class MetricTrigger:
    priority: int
    severity: str
    code: str
    metric: str
    value: float
    threshold: str
    message_th: str
    device_message: str
    evidence_note: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class MetricTriggerEngine:
    """Evaluates deterministic running-form thresholds."""

    def evaluate(
        self, features: Mapping[str, float], diagnostics: Mapping[str, float]
    ) -> list[MetricTrigger]:
        triggers: list[MetricTrigger] = []
        cadence = features["cadence_spm"]
        bounce = features["vertical_oscillation_cm"]
        lean = features["trunk_forward_lean_deg"]
        asymmetry = features["left_right_asymmetry_pct"]
        gct = diagnostics.get("gct_ms", 0.0)
        impact = diagnostics.get("peak_vgrf_bw_estimate", 0.0)
        strike_time = diagnostics.get("footstrike_time_to_peak_ms", 0.0)
        arm_swing_proxy = diagnostics.get("arm_swing_proxy")

        if impact > VGRF_DANGER_BW:
            triggers.append(
                MetricTrigger(
                    1,
                    "ALERT",
                    "impact_estimate_very_high",
                    "peak_vgrf_bw_estimate",
                    impact,
                    "> 2.5x BW",
                    "แรงกระแทกสูงมาก ลงเท้าเบาๆ ⚠️",
                    "Impact very high; land softly.",
                    "Priority 1 danger rule from estimated peak vGRF.",
                )
            )
        elif impact > VGRF_WARNING_BW:
            triggers.append(
                MetricTrigger(
                    2,
                    "WARN",
                    "impact_estimate_high",
                    "peak_vgrf_bw_estimate",
                    impact,
                    "> 2.0x BW",
                    "แรงกระแทกเริ่มสูง ระวังด้วยนะ",
                    "Impact rising; be careful and land softer.",
                    "Priority 2 warning rule from estimated peak vGRF.",
                )
            )
        if asymmetry > ASYMMETRY_HIGH_PCT:
            triggers.append(
                MetricTrigger(
                    2,
                    "WARN",
                    "asymmetry_high",
                    "left_right_asymmetry_pct",
                    asymmetry,
                    "> 10%",
                    "ลงน้ำหนักไม่เท่ากัน เช็คขาซ้าย/ขวา ↔️",
                    "Left-right loading uneven; check both sides.",
                    "Priority 2 warning rule from left/right asymmetry proxy.",
                )
            )
        if lean < LEAN_LOW_DEG:
            triggers.append(
                MetricTrigger(
                    2,
                    "WARN",
                    "lean_low",
                    "trunk_forward_lean_deg",
                    lean,
                    "< 3 deg",
                    "เอนตัวไปข้างหน้านิดนึง 📐",
                    "Lean forward slightly.",
                    "Priority 2 warning rule from trunk forward lean.",
                )
            )
        elif lean > LEAN_HIGH_DEG:
            triggers.append(
                MetricTrigger(
                    2,
                    "WARN",
                    "lean_high",
                    "trunk_forward_lean_deg",
                    lean,
                    "> 15 deg",
                    "ตั้งตัวขึ้นหน่อย เอนเกินไปแล้ว",
                    "Stand a little taller; lean is too high.",
                    "Priority 2 warning rule from trunk forward lean.",
                )
            )
        if gct > GCT_HIGH_MS:
            triggers.append(
                MetricTrigger(
                    2,
                    "WARN",
                    "gct_long",
                    "gct_ms",
                    gct,
                    "> 300 ms",
                    "ยกเท้าให้ไวขึ้น เท้าค้างพื้นนานเกิน ⚡",
                    "Lift the foot quicker; contact time is long.",
                    "Priority 2 warning rule from estimated ground-contact time.",
                )
            )
        if bounce > VERTICAL_OSCILLATION_HIGH_CM:
            triggers.append(
                MetricTrigger(
                    3,
                    "WARN",
                    "bounce_high",
                    "vertical_oscillation_cm",
                    bounce,
                    "> 10 cm",
                    "วิ่งไปข้างหน้า อย่ากระโดด 🏃",
                    "Run forward; reduce bouncing.",
                    "Priority 3 improve rule from vertical oscillation.",
                )
            )
        elif bounce < VERTICAL_OSCILLATION_LOW_CM:
            triggers.append(
                MetricTrigger(
                    3,
                    "WARN",
                    "bounce_low",
                    "vertical_oscillation_cm",
                    bounce,
                    "< 4 cm",
                    "ยกเท้าขึ้นบ้าง ก้าวติดพื้นเกินไป",
                    "Lift the feet a bit; stride is too flat.",
                    "Priority 3 improve rule from vertical oscillation.",
                )
            )
        if cadence < CADENCE_LOW_SPM:
            triggers.append(
                MetricTrigger(
                    3,
                    "WARN",
                    "cadence_low",
                    "cadence_spm",
                    cadence,
                    "< 160 spm",
                    "ก้าวถี่ขึ้นหน่อย 🦶",
                    "Increase cadence a little.",
                    "Priority 3 improve rule from cadence.",
                )
            )
        elif cadence > CADENCE_HIGH_SPM:
            triggers.append(
                MetricTrigger(
                    3,
                    "WARN",
                    "cadence_high",
                    "cadence_spm",
                    cadence,
                    "> 200 spm",
                    "ก้าวช้าลงนิดนึง ประหยัดแรง",
                    "Slow cadence slightly to save energy.",
                    "Priority 3 improve rule from cadence.",
                )
            )
        if 0.0 < strike_time < HEEL_STRIKE_RISE_TIME_MS:
            triggers.append(
                MetricTrigger(
                    3,
                    "WARN",
                    "sharp_footstrike_proxy",
                    "footstrike_time_to_peak_ms",
                    strike_time,
                    "< 15 ms",
                    "ลองลงกลางเท้าแทน ลดแรงกระแทกได้เยอะ 👟",
                    "Try a more midfoot landing.",
                    "Priority 3 improve rule from impact rise time proxy.",
                )
            )
        if arm_swing_proxy is not None and arm_swing_proxy < ARM_SWING_PROXY_LOW:
            triggers.append(
                MetricTrigger(
                    4,
                    "INFO",
                    "arm_swing_proxy_low",
                    "arm_swing_proxy",
                    float(arm_swing_proxy),
                    f"< {ARM_SWING_PROXY_LOW:g}",
                    "แกว่งแขนน้อยไป ช่วยส่งจังหวะอีกนิด",
                    "Arm swing proxy low; add a little rhythm.",
                    "Priority 4 tip rule; evaluated only when arm_swing_proxy is supplied.",
                )
            )
        return sorted(triggers, key=lambda trigger: (trigger.priority, trigger.code))
