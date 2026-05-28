import csv
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "python"))

from formsense_pipeline.filters import Calibration, SensorFilter
from formsense_pipeline.feedback import FeedbackEngine
from formsense_pipeline.metric_triggers import MetricTriggerEngine
from formsense_pipeline.pipeline import RunningFormPipeline
from formsense_pipeline.protocol import ImuSample, ProtocolError, encode_feature, encode_imu, parse_imu
from simulate_session import samples


class ProtocolTests(unittest.TestCase):
    def test_imu_packet_round_trip_and_crc_failure(self):
        sample = next(samples(1, 200, 168.0, False))
        encoded = encode_imu(sample)
        self.assertEqual(parse_imu(encoded).seq, sample.seq)
        with self.assertRaises(ProtocolError):
            parse_imu(encoded[:-1] + ("0" if encoded[-1] != "0" else "1"))

    def test_feature_packet_fits_bridge_envelope(self):
        values = {
            "cadence_spm": 175.0,
            "vertical_oscillation_cm": 9.1,
            "gct_flight_balance_ms": -22.0,
            "impact_loading_rate_bw_s": 14.5,
            "trunk_forward_lean_deg": 9.0,
            "left_right_asymmetry_pct": 3.8,
            "heel_strike_likelihood": 0.25,
        }
        self.assertLess(len(encode_feature(100, 123.4, values, "GOOD_FORM")), 190)


class FilterTests(unittest.TestCase):
    def test_gyro_bias_is_removed(self):
        calibration = Calibration(gyro_bias_dps=(1.0, -2.0, 0.5))
        sensor_filter = SensorFilter(calibration)
        sample = ImuSample(1, 0.01, 0, 0, 1, 1.0, -2.0, 0.5)
        output = sensor_filter.process(sample)
        self.assertAlmostEqual(output["gyro_x_cal_dps"], 0.0, places=5)
        self.assertAlmostEqual(output["gyro_y_cal_dps"], 0.0, places=5)


class PipelineTests(unittest.TestCase):
    def test_synthetic_run_writes_labeled_features_with_expected_cadence(self):
        with tempfile.TemporaryDirectory() as directory:
            pipeline = RunningFormPipeline(Path(directory), "test", "GOOD_FORM")
            final = None
            for sample in samples(12, 200, 168.0, False):
                features = pipeline.ingest(sample)
                if features:
                    final = features
            pipeline.close()
            self.assertIsNotNone(final)
            self.assertGreater(final["cadence_spm"], 145)
            self.assertLess(final["cadence_spm"], 190)
            with pipeline.recorder.feature_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertGreater(len(rows), 3)
            self.assertTrue(all(row["form_label"] == "GOOD_FORM" for row in rows))
            self.assertTrue(pipeline.recorder.raw_path.exists())
            self.assertTrue(pipeline.recorder.filtered_path.exists())


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.baseline = {
            "features": {
                "cadence_spm": {"mean": 170.0},
                "vertical_oscillation_cm": {"mean": 8.0},
                "impact_loading_rate_bw_s": {"mean": 4.0},
                "trunk_forward_lean_deg": {"mean": 8.0},
                "left_right_asymmetry_pct": {"mean": 4.0},
            }
        }
        self.bad_values = {
            "cadence_spm": 145.0,
            "vertical_oscillation_cm": 12.0,
            "gct_flight_balance_ms": 20.0,
            "impact_loading_rate_bw_s": 8.0,
            "trunk_forward_lean_deg": 19.0,
            "left_right_asymmetry_pct": 20.0,
            "heel_strike_likelihood": 0.8,
        }

    def test_realtime_alert_is_baseline_based_and_rate_limited(self):
        feedback = FeedbackEngine(self.baseline, mode="realtime", realtime_cooldown_s=20.0)
        self.assertEqual(feedback.ingest(3.0, self.bad_values)[0].severity, "ALERT")
        self.assertEqual(feedback.ingest(4.0, self.bad_values), [])
        self.assertEqual(len(feedback.ingest(24.0, self.bad_values)), 1)

    def test_five_min_summary_emits_at_interval(self):
        feedback = FeedbackEngine(self.baseline, mode="five_min", summary_interval_s=300.0)
        self.assertEqual(feedback.ingest(3.0, self.bad_values), [])
        events = feedback.ingest(303.0, self.bad_values)
        self.assertEqual(events[0].event_type, "FIVE_MIN_SUMMARY")
        self.assertEqual(events[0].severity, "ALERT")


class MetricTriggerTests(unittest.TestCase):
    def test_high_estimated_impact_has_top_priority_and_uses_proxy_language(self):
        features = {
            "cadence_spm": 150.0,
            "vertical_oscillation_cm": 11.0,
            "gct_flight_balance_ms": 40.0,
            "impact_loading_rate_bw_s": 24.0,
            "trunk_forward_lean_deg": 16.0,
            "left_right_asymmetry_pct": 12.0,
            "heel_strike_likelihood": 0.8,
        }
        diagnostics = {
            "gct_ms": 320.0,
            "peak_vgrf_bw_estimate": 2.7,
            "footstrike_time_to_peak_ms": 10.0,
        }
        triggers = MetricTriggerEngine().evaluate(features, diagnostics)
        self.assertEqual(triggers[0].code, "impact_estimate_very_high")
        self.assertEqual(triggers[0].severity, "ALERT")
        self.assertIn("estimate", triggers[0].device_message.lower())

    def test_sharp_footstrike_is_information_not_forced_retraining(self):
        features = {
            "cadence_spm": 175.0,
            "vertical_oscillation_cm": 7.0,
            "gct_flight_balance_ms": 20.0,
            "impact_loading_rate_bw_s": 5.0,
            "trunk_forward_lean_deg": 8.0,
            "left_right_asymmetry_pct": 4.0,
            "heel_strike_likelihood": 0.8,
        }
        triggers = MetricTriggerEngine().evaluate(
            features,
            {"gct_ms": 250.0, "peak_vgrf_bw_estimate": 1.5, "footstrike_time_to_peak_ms": 10.0},
        )
        self.assertEqual(triggers[0].code, "sharp_footstrike_proxy")
        self.assertEqual(triggers[0].severity, "INFO")


if __name__ == "__main__":
    unittest.main()
