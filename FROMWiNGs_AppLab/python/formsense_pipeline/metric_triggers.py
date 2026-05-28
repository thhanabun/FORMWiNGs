"""Rule-based coaching messages for interpretable running-form metrics."""

from dataclasses import asdict, dataclass
from typing import Mapping


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
    """Evaluates prototype triggers; messages are awareness cues, not diagnoses."""

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

        if impact > 2.5:
            triggers.append(
                MetricTrigger(
                    1,
                    "ALERT",
                    "impact_estimate_very_high",
                    "peak_vgrf_bw_estimate",
                    impact,
                    "> 2.5 BW",
                    "แรงกระแทกประมาณการสูงมาก ลองลงเท้าให้นุ่มและใต้ลำตัวมากขึ้น",
                    "Impact estimate high; soften landing under your body.",
                    "Estimated from waist acceleration; validate against force or foot sensor.",
                )
            )
        elif impact > 2.0:
            triggers.append(
                MetricTrigger(
                    2,
                    "WARN",
                    "impact_estimate_high",
                    "peak_vgrf_bw_estimate",
                    impact,
                    "> 2.0 BW",
                    "แรงกระแทกประมาณการเริ่มสูง ลองลดการก้าวยื่นและลงเบาขึ้น",
                    "Impact estimate rising; shorten overstride and land softly.",
                    "Estimated from waist acceleration; not measured ground force.",
                )
            )
        if asymmetry > 10.0:
            triggers.append(
                MetricTrigger(
                    2,
                    "WARN",
                    "asymmetry_high",
                    "left_right_asymmetry_pct",
                    asymmetry,
                    "> 10%",
                    "รูปแบบซ้าย-ขวาไม่สมดุล เช็คว่าสายคาดแน่นตรงกลางและปรับจังหวะก้าว",
                    "Left-right proxy uneven; check belt fit and step balance.",
                    "Alternating waist-impact proxy; confirm left/right with reference data.",
                )
            )
        if gct > 300.0:
            triggers.append(
                MetricTrigger(
                    3,
                    "WARN",
                    "gct_long",
                    "gct_ms",
                    gct,
                    "> 300 ms",
                    "เท้าแตะพื้นนานขึ้น ลองก้าวให้เบาและคืนเท้าไวขึ้น",
                    "Contact-time proxy long; use lighter quicker steps.",
                    "GCT is estimated at the waist; foot-mounted validation is recommended.",
                )
            )
        if lean > 15.0:
            triggers.append(
                MetricTrigger(
                    3,
                    "WARN",
                    "lean_high",
                    "trunk_forward_lean_deg",
                    lean,
                    "> 15 deg",
                    "ลำตัวเอนไปข้างหน้ามาก ลองจัดแนวลำตัวใหม่จากข้อเท้า",
                    "Forward lean high; reset torso alignment from the ankles.",
                    "Requires correctly mounted and neutral-calibrated waist sensor.",
                )
            )
        if bounce > 10.0:
            triggers.append(
                MetricTrigger(
                    4,
                    "WARN",
                    "bounce_high",
                    "vertical_oscillation_cm",
                    bounce,
                    "> 10 cm",
                    "ตัวเด้งมากขึ้น ลองส่งแรงไปข้างหน้าและก้าวให้เงียบลง",
                    "Bounce elevated; keep steps compact and forward.",
                    "Vertical oscillation is an inertial estimate.",
                )
            )
        if cadence < 160.0:
            triggers.append(
                MetricTrigger(
                    5,
                    "INFO",
                    "cadence_low",
                    "cadence_spm",
                    cadence,
                    "< 160 spm",
                    "รอบขาต่ำกว่าช่วงอ้างอิง ลองก้าวสั้นและถี่ขึ้นเล็กน้อย",
                    "Cadence low; try slightly shorter quicker steps.",
                    "Cadence target depends on pace and individual baseline.",
                )
            )
        elif cadence > 200.0:
            triggers.append(
                MetricTrigger(
                    5,
                    "INFO",
                    "cadence_high",
                    "cadence_spm",
                    cadence,
                    "> 200 spm",
                    "รอบขาสูงมาก เช็คว่ากำลังเกร็งหรือเร่งเกินเป้าหมายหรือไม่",
                    "Cadence high; check unnecessary tension or pace.",
                    "Cadence target depends on pace and individual baseline.",
                )
            )
        if 0.0 < strike_time < 15.0:
            triggers.append(
                MetricTrigger(
                    6,
                    "INFO",
                    "sharp_footstrike_proxy",
                    "footstrike_time_to_peak_ms",
                    strike_time,
                    "< 15 ms",
                    "สัญญาณลงเท้าคม ลองลดการก้าวยื่นและลงเท้านุ่มขึ้น",
                    "Sharp foot-strike proxy; reduce overstride and soften landing.",
                    "Waist IMU cannot confirm heel strike alone; do not force a strike change.",
                )
            )
        return sorted(triggers, key=lambda trigger: (trigger.priority, trigger.code))
