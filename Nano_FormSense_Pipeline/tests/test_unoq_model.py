import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "python"))

try:
    from formsense_pipeline.unoq_model import BiomechanicsExtractor

    EXTRACTION_DEPS_AVAILABLE = True
except ModuleNotFoundError:
    EXTRACTION_DEPS_AVAILABLE = False

try:
    from ai_edge_litert.interpreter import Interpreter  # noqa: F401

    LITERT_AVAILABLE = True
except ImportError:
    try:
        from tflite_runtime.interpreter import Interpreter  # noqa: F401

        LITERT_AVAILABLE = True
    except ImportError:
        LITERT_AVAILABLE = False

from formsense_pipeline.protocol import FEATURE_KEYS
from formsense_pipeline.bluetooth_delivery import BluetoothAlertDelivery
from simulate_session import samples


class UNOQModelTests(unittest.TestCase):
    @unittest.skipUnless(EXTRACTION_DEPS_AVAILABLE, "UNO Q numpy/scipy dependencies are not installed")
    def test_reference_extractor_emits_seven_features(self):
        values = list(samples(12.0, 200, 168.0, True))
        extractor = BiomechanicsExtractor()
        features = extractor.extract_latest(values)
        self.assertIsNotNone(features)
        self.assertEqual(extractor.config.window_s, 5.0)
        self.assertEqual(set(features), set(FEATURE_KEYS))
        self.assertGreater(features["cadence_spm"], 100.0)

    @unittest.skipUnless(EXTRACTION_DEPS_AVAILABLE, "UNO Q numpy/scipy dependencies are not installed")
    def test_extractor_exposes_rule_diagnostics_without_changing_model_features(self):
        values = list(samples(12.0, 200, 168.0, True))
        features, diagnostics = BiomechanicsExtractor().extract_latest_with_diagnostics(values)
        self.assertEqual(set(features), set(FEATURE_KEYS))
        self.assertIn("gct_ms", diagnostics)
        self.assertIn("peak_vgrf_bw_estimate", diagnostics)
        self.assertIn("footstrike_time_to_peak_ms", diagnostics)

    @unittest.skipUnless(
        EXTRACTION_DEPS_AVAILABLE and LITERT_AVAILABLE,
        "UNO Q numpy/scipy/LiteRT dependencies are not installed",
    )
    def test_supplied_tflite_returns_normalized_attention_weights(self):
        from formsense_pipeline.unoq_model import RunningFormPredictor

        model_path = ROOT / "model" / "running_form_transformer_fp16.tflite"
        if not model_path.exists():
            self.skipTest("supplied TFLite model is absent")
        values = list(samples(12.0, 200, 168.0, True))
        features = BiomechanicsExtractor().extract_latest(values)
        output = RunningFormPredictor(model_path).predict(features)
        self.assertEqual(set(output["attention_weights"]), set(FEATURE_KEYS))
        self.assertAlmostEqual(sum(output["attention_weights"].values()), 1.0, places=4)
        self.assertFalse(output["production_ready"])


class BluetoothDeliveryTests(unittest.TestCase):
    def prediction(self):
        return {
            "timestamp_s": 10.0,
            "priority_trigger": {
                "code": "asymmetry_high",
                "severity": "WARN",
                "message_th": "รูปแบบซ้าย-ขวาไม่สมดุล",
            },
            "feedback": {"alert_source": "METRIC_TRIGGER"},
        }

    def test_alert_is_stored_when_ble_is_not_configured(self):
        with tempfile.TemporaryDirectory() as directory:
            delivery = BluetoothAlertDelivery(Path(directory), "run", None, None)
            result = delivery.deliver_prediction(self.prediction())
            self.assertEqual(result["status"], "STORED_LOCAL")
            self.assertIn("asymmetry_high", delivery.outbox_path.read_text(encoding="utf-8"))

    def test_pending_alert_is_retried_after_ble_becomes_available(self):
        with tempfile.TemporaryDirectory() as directory:
            first = BluetoothAlertDelivery(Path(directory), "run", None, None)
            first.deliver_prediction(self.prediction())
            delivery = BluetoothAlertDelivery(Path(directory), "run", "AA:BB", "char-uuid")
            delivery._try_send = AsyncMock(return_value=(True, "BLE_GATT_WRITE_OK"))
            result = delivery.retry_pending()
            self.assertEqual(result, {"sent": 1, "remaining": 0})
            self.assertEqual(delivery.outbox_path.read_text(encoding="utf-8"), "")


if __name__ == "__main__":
    unittest.main()
