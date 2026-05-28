# FormSense UNO Q Package - Team Handoff

ส่งให้เพื่อนทั้งโฟลเดอร์นี้:

```text
Nano_FormSense_Pipeline/
```

โฟลเดอร์มี Nano firmware, UNO Q pipeline และโมเดล deploy แล้ว:

```text
nano/nano_33_ble_sense_rev2_sender.ino
python/uno_q_live_inference.py
python/formsense_pipeline/unoq_model.py
python/formsense_pipeline/metric_triggers.py
python/formsense_pipeline/bluetooth_delivery.py
model/running_form_transformer_fp16.tflite
```

## Data Flow

```text
Waist Nano accel+gyro @ 200 Hz
  -> USB Serial / UART to UNO Q Linux
  -> 5-second sliding window, update every 1 second
  -> seven features
  -> TFLite Good/Bad Form + feature_attention
  -> metric priority trigger
  -> BLE alert to receiving device
  -> local outbox when BLE is unavailable
```

## UNO Q Setup

```bash
cd Nano_FormSense_Pipeline
python3 -m venv .venv
source .venv/bin/activate
pip install -r python/requirements_unoq.txt
```

## Run

```bash
PYTHONPATH=python python3 python/uno_q_live_inference.py \
  --port /dev/ttyACM0 --baud 460800 \
  --calibration calibration.json \
  --enable-metric-alerts \
  --ble-address "TARGET_DEVICE_ADDRESS" \
  --ble-characteristic "WRITABLE_GATT_CHARACTERISTIC_UUID" \
  --output-dir ~/formsense_data/live
```

โมเดล `model/running_form_transformer_fp16.tflite` ถูกโหลดเป็น default แล้ว.
เมื่อได้รับ `running_form_normalizer.json` จาก training pipeline ให้ใส่ไฟล์ไว้
ใน `model/` และเพิ่ม `--normalizer model/running_form_normalizer.json`.

## Bluetooth Contract

อุปกรณ์รับต้อง advertise BLE และเปิด writable GATT characteristic. ค่า
`TARGET_DEVICE_ADDRESS` อาจเป็น MAC address หรือ platform identifier ตาม
BlueZ/BLE environment ของ UNO Q. UNO Q ส่ง
compact JSON เฉพาะตอนเกิด `WARN` หรือ `ALERT`:

```json
{"type":"formsense_alert","ts":10.0,"severity":"WARN","code":"asymmetry_high"}
```

ถ้าส่งไม่ได้ ระบบบันทึกไว้ใน:

```text
~/formsense_data/live/<session-id>_ble_outbox.jsonl
```

## Current Safety Constraint

`running_form_normalizer.json` จาก training เดิมยังไม่มีใน package.
Model output/attention ใช้ทดสอบ integration ได้ แต่ model-probability alert
ยังถูกปิด. `--enable-metric-alerts` เปิด alert จากกฎ prototype โดยตรงและต้อง
validate thresholds กับการวิ่งจริงก่อน demo กับผู้ใช้.
