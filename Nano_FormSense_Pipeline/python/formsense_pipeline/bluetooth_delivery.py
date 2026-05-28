"""BLE alert delivery with a durable local outbox for disconnected devices."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping


class BluetoothAlertDelivery:
    """Writes compact alert records to a BLE characteristic or stores them locally."""

    def __init__(
        self,
        output_dir: Path,
        session_id: str,
        address: str | None,
        characteristic: str | None,
        scan_timeout_s: float = 3.0,
    ):
        self.address = address
        self.characteristic = characteristic
        self.scan_timeout_s = scan_timeout_s
        self.outbox_path = output_dir / f"{session_id}_ble_outbox.jsonl"
        self.delivery_log_path = output_dir / f"{session_id}_ble_delivery.jsonl"

    @property
    def configured(self) -> bool:
        return bool(self.address and self.characteristic)

    def deliver_prediction(self, prediction: Mapping[str, object]) -> dict[str, object]:
        notification = self._notification(prediction)
        if self.configured:
            sent, reason = asyncio.run(self._try_send(notification))
        else:
            sent, reason = False, "BLE_NOT_CONFIGURED"
        if sent:
            self._log("BLE_SENT", notification, reason)
            retry = self.retry_pending()
            return {"status": "BLE_SENT", "reason": reason, "retried": retry["sent"]}
        self._append_outbox(notification)
        self._log("STORED_LOCAL", notification, reason)
        return {"status": "STORED_LOCAL", "reason": reason, "outbox": str(self.outbox_path)}

    def retry_pending(self) -> dict[str, int]:
        if not self.configured or not self.outbox_path.exists():
            return {"sent": 0, "remaining": self._outbox_count()}
        pending = [
            json.loads(line)
            for line in self.outbox_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        unsent: list[dict[str, object]] = []
        sent_count = 0
        for notification in pending:
            sent, reason = asyncio.run(self._try_send(notification))
            if sent:
                sent_count += 1
                self._log("RETRY_BLE_SENT", notification, reason)
            else:
                unsent.append(notification)
                self._log("RETRY_FAILED", notification, reason)
                unsent.extend(pending[len(unsent) + sent_count :])
                break
        content = "".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n" for item in unsent)
        self.outbox_path.write_text(content, encoding="utf-8")
        return {"sent": sent_count, "remaining": len(unsent)}

    @staticmethod
    def _notification(prediction: Mapping[str, object]) -> dict[str, object]:
        trigger = prediction.get("priority_trigger")
        feedback = prediction.get("feedback", {})
        if isinstance(trigger, Mapping):
            code = str(trigger["code"])
            severity = str(trigger["severity"])
            message_th = str(trigger["message_th"])
        else:
            code = "model_bad_form"
            severity = "ALERT"
            message_th = "ตรวจพบรูปแบบการวิ่งที่ควรปรับจากโมเดล"
        return {
            "type": "formsense_alert",
            "timestamp_s": prediction["timestamp_s"],
            "severity": severity,
            "code": code,
            "message_th": message_th,
            "source": feedback.get("alert_source", "MODEL") if isinstance(feedback, Mapping) else "MODEL",
        }

    async def _try_send(self, notification: Mapping[str, object]) -> tuple[bool, str]:
        try:
            from bleak import BleakClient, BleakScanner
        except ImportError:
            return False, "BLEAK_NOT_INSTALLED"
        try:
            device = await BleakScanner.find_device_by_address(self.address, timeout=self.scan_timeout_s)
            if device is None:
                return False, "BLE_TARGET_NOT_FOUND"
            compact = {
                "type": notification["type"],
                "ts": notification["timestamp_s"],
                "severity": notification["severity"],
                "code": notification["code"],
            }
            data = json.dumps(compact, separators=(",", ":")).encode("utf-8")
            async with BleakClient(device, timeout=self.scan_timeout_s) as client:
                if not client.is_connected:
                    return False, "BLE_NOT_CONNECTED"
                await client.write_gatt_char(self.characteristic, data, response=True)
            return True, "BLE_GATT_WRITE_OK"
        except Exception as error:
            return False, f"BLE_SEND_ERROR:{type(error).__name__}"

    def _append_outbox(self, notification: Mapping[str, object]) -> None:
        with self.outbox_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(notification, ensure_ascii=False, separators=(",", ":")) + "\n")

    def _log(self, status: str, notification: Mapping[str, object], reason: str) -> None:
        row = {
            "logged_utc": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "reason": reason,
            "notification": notification,
        }
        with self.delivery_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")

    def _outbox_count(self) -> int:
        if not self.outbox_path.exists():
            return 0
        return sum(1 for line in self.outbox_path.read_text(encoding="utf-8").splitlines() if line.strip())
