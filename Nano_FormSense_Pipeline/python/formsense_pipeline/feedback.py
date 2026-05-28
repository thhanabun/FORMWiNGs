"""Personal-baseline feedback for a waist-mounted running form wearable."""

import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class FeedbackEvent:
    timestamp_s: float
    event_type: str
    severity: str
    message: str


class FeedbackEngine:
    """Produces coaching cues relative to the runner's verified good baseline."""

    def __init__(
        self,
        baseline: Mapping[str, object],
        mode: str = "both",
        realtime_cooldown_s: float = 20.0,
        summary_interval_s: float = 300.0,
    ):
        if mode not in {"off", "realtime", "five_min", "both"}:
            raise ValueError(f"unsupported feedback mode: {mode}")
        self.feature_baseline = baseline.get("features", {})
        if not self.feature_baseline:
            raise ValueError("baseline does not contain features")
        self.mode = mode
        self.realtime_cooldown_s = realtime_cooldown_s
        self.summary_interval_s = summary_interval_s
        self.last_realtime_s = float("-inf")
        self.next_summary_s: float | None = None
        self.summary_values: list[Mapping[str, float]] = []

    @classmethod
    def load(
        cls,
        path: Path,
        mode: str = "both",
        realtime_cooldown_s: float = 20.0,
        summary_interval_s: float = 300.0,
    ) -> "FeedbackEngine":
        return cls(
            json.loads(path.read_text(encoding="utf-8")),
            mode=mode,
            realtime_cooldown_s=realtime_cooldown_s,
            summary_interval_s=summary_interval_s,
        )

    def _mean(self, key: str, fallback: float) -> float:
        item = self.feature_baseline.get(key, {})
        return float(item.get("mean", fallback)) if isinstance(item, Mapping) else fallback

    @staticmethod
    def _highest(issues: list[tuple[str, str]]) -> str:
        return "ALERT" if any(severity == "ALERT" for severity, _ in issues) else "WARN"

    def _issues(self, values: Mapping[str, float]) -> list[tuple[str, str]]:
        issues: list[tuple[str, str]] = []
        cadence_drop = self._mean("cadence_spm", 170.0) - values["cadence_spm"]
        lean_delta = values["trunk_forward_lean_deg"] - self._mean("trunk_forward_lean_deg", 8.0)
        oscillation_base = self._mean("vertical_oscillation_cm", 8.0)
        impact_base = self._mean("impact_loading_rate_bw_s", 4.0)
        asym_base = self._mean("left_right_asymmetry_pct", 4.0)

        if cadence_drop >= 12.0:
            issues.append(("ALERT", "Cadence dropped; shorten stride and increase step rhythm gently."))
        elif cadence_drop >= 6.0:
            issues.append(("WARN", "Cadence is falling; avoid overstriding."))
        if lean_delta >= 8.0:
            issues.append(("ALERT", "Forward lean increased; align torso from ankles and reset posture."))
        elif lean_delta >= 5.0:
            issues.append(("WARN", "Forward lean elevated; check torso posture."))
        if values["vertical_oscillation_cm"] > max(oscillation_base * 1.30, oscillation_base + 2.0):
            issues.append(("WARN", "Vertical bounce increased; aim for quieter compact steps."))
        if values["impact_loading_rate_bw_s"] > max(impact_base * 1.45, impact_base + 0.8):
            issues.append(("WARN", "Impact proxy increased; soften landing and avoid reaching forward."))
        if values["left_right_asymmetry_pct"] > max(asym_base + 10.0, 16.0):
            issues.append(("WARN", "Left/right proxy is uneven; check belt position and step balance."))
        return issues

    def ingest(self, timestamp_s: float, features: Mapping[str, float]) -> list[FeedbackEvent]:
        if self.mode == "off":
            return []
        if self.next_summary_s is None:
            self.next_summary_s = timestamp_s + self.summary_interval_s
        self.summary_values.append(features)
        events: list[FeedbackEvent] = []
        issues = self._issues(features)

        if (
            self.mode in {"realtime", "both"}
            and issues
            and timestamp_s - self.last_realtime_s >= self.realtime_cooldown_s
        ):
            severity = self._highest(issues)
            message = " ".join(text for _, text in issues[:2])
            events.append(FeedbackEvent(timestamp_s, "REALTIME", severity, message))
            self.last_realtime_s = timestamp_s

        if self.mode in {"five_min", "both"} and timestamp_s >= self.next_summary_s:
            average = {
                key: statistics.fmean(item[key] for item in self.summary_values)
                for key in features
            }
            summary_issues = self._issues(average)
            if summary_issues:
                severity = self._highest(summary_issues)
                message = "5-minute trend: " + " ".join(text for _, text in summary_issues[:2])
            else:
                severity = "GOOD"
                message = "5-minute trend: form remains close to your verified baseline."
            events.append(FeedbackEvent(timestamp_s, "FIVE_MIN_SUMMARY", severity, message))
            self.summary_values.clear()
            while timestamp_s >= self.next_summary_s:
                self.next_summary_s += self.summary_interval_s
        return events
