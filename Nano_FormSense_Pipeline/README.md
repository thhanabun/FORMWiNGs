# FormSense Waist Running-Form Pipeline

Pipeline สำหรับอุปกรณ์ **คาดเอว** ที่มี `accelerometer + gyroscope` บน
Arduino Nano: เก็บ raw data, filter, สกัด 7 features, บันทึกข้อมูลสำหรับ train
classifier และแจ้งเตือนผู้วิ่งแบบ realtime หรือสรุปทุก 5 นาที

```text
Waist Nano IMU -> UART/USB -> Python collector -> raw / filtered / features CSV
                                           -> baseline coaching + alerts CSV
                                           -> sync files to UNO Q eMMC
```

## สิ่งที่วัดได้จากตำแหน่งเอว

| Feature | ความหมายใน prototype | ข้อจำกัด |
| --- | --- | --- |
| `cadence_spm` | จำนวนก้าวจาก vertical peaks | ควรเทียบ video/footpod |
| `vertical_oscillation_cm` | การเด้งตัวจาก integration ของ trunk acceleration | เป็น estimate และมี drift |
| `gct_flight_balance_ms` | timing proxy จากช่วงแรงกระแทก/ลอยตัว | เอวไม่แม่นเท่า sensor ที่เท้าหรือ tibia |
| `impact_loading_rate_bw_s` | waist impact proxy | ไม่ใช่ ground reaction force |
| `trunk_forward_lean_deg` | มุมลำตัวเทียบกับท่ายืน calibration | เหมาะกับ sensor ที่คาดเอวแน่น |
| `left_right_asymmetry_pct` | alternating impact proxy | ต้อง validate foot side ด้วย video |
| `heel_strike_likelihood` | heuristic จากความคมของ impact | เอวยืนยันชนิด foot strike ไม่ได้ |

ดังนั้น alert ใช้วิธีตรวจการเปลี่ยนจาก **personal GOOD_FORM baseline** ไม่
วินิจฉัยการบาดเจ็บหรือประกาศว่าฟอร์มถูก/ผิดโดยไม่มี coach/video label.
เหตุผลของข้อจำกัดนี้สอดคล้องกับงาน validation ของ torso-mounted accelerometer
สำหรับ vertical oscillation/GCT และ systematic review ที่พบว่า gait event
เช่น heel strike มัก validate ด้วย IMU บริเวณ foot/shank และ insole pressure.

## Sampling และ Filtering

`200 Hz` เป็น target ที่เหมาะสำหรับเก็บรายละเอียด transient เพื่อทำ dataset
แต่ต้องตั้ง ODR ของชิปให้ได้จริงและตรวจ timestamp/packet loss. สำหรับ wearable
คาดเอว ถ้า library ของบอร์ดส่งจริงได้เพียงประมาณ `100 Hz` ยังใช้วัด cadence
และ trunk lean ได้ เพียงต้องบันทึก sampling rate จริงไว้กับ experiment.

| Channel | Default pipeline |
| --- | --- |
| Accelerometer posture | low-pass `5 Hz` สำหรับ roll/pitch |
| Accelerometer dynamic/impact proxy | low-pass `35 Hz`, แล้ว high-pass `0.5 Hz` |
| Gyroscope | subtract stationary bias, low-pass `20 Hz` |
| Trunk orientation | complementary fusion ของ accel + gyro เฉพาะ roll/pitch |
| Window | `5.0 s`, stride `1.0 s` |

เริ่มทดสอบ range ของ accelerometer ที่ `+/-4 g` สำหรับตำแหน่งเอวและตรวจว่า
ไม่ clipping; เปลี่ยนเป็น `+/-8 g` หากพบ peak ชนขอบ range. Gyroscope ใช้
`+/-500` หรือ `+/-1000 dps` ก่อนสำหรับลำตัว และเพิ่ม range หากพบ clipping.
`+/-8 g` กับ `+/-2000 dps` ไม่ผิด แต่ resolution จะลดลงโดยไม่จำเป็นหากแรง
เคลื่อนไหวที่เอวไม่ถึง range นั้น.

## Files

```text
nano/nano_33_ble_sense_rev2_sender.ino  sender สำหรับ BMI270 accel/gyro onboard
nano/nano_imu_uart_sender.ino           template สำหรับ Nano + IMU รุ่นอื่น
python/main.py                          serial collector + live alerts
python/calibrate.py                     gyro bias + neutral waist pitch
python/build_baseline.py                personal GOOD_FORM baseline
python/build_training_dataset.py        รวม labeled windows สำหรับ training
python/formsense_pipeline/              protocol, filters, extraction, feedback
python/formsense_pipeline/unoq_model.py UNO Q model-matched extraction + LiteRT attention
python/formsense_pipeline/bluetooth_delivery.py BLE delivery + durable local outbox
python/uno_q_live_inference.py          Nano -> UNO Q TFLite runner
model/running_form_transformer_fp16.tflite bundled UNO Q deployment model
python/simulate_session.py              hardware-free end-to-end demo
scripts/sync_dataset_to_unoq.sh         ส่ง CSV ไป UNO Q ผ่าน SSH
```

## 1. Nano Sender

สำหรับ **Nano 33 BLE Sense Rev2** ใช้
[nano/nano_33_ble_sense_rev2_sender.ino](nano/nano_33_ble_sense_rev2_sender.ino)
และ library `Arduino_BMI270_BMM150`. ชื่อ library มี BMM150 เพราะ package ของ
บอร์ดรวม sensor ไว้ แต่ protocol นี้ส่งเฉพาะ BMI270 accelerometer/gyroscope:

```text
timestamp_s, acc_x_g, acc_y_g, acc_z_g, gyro_x_dps, gyro_y_dps, gyro_z_dps
```

หากต้องการรับรอง `200 Hz` ต้องใช้ configuration/driver ที่ตั้ง BMI270 ODR
เป็น 200 Hz ได้จริง; ตรวจจำนวน sample ต่อวินาทีจาก CSV เสมอ.
ดู packet format ใน [UART_PROTOCOL.md](UART_PROTOCOL.md).

## 2. Install

```bash
cd "/Volumes/Drive D/SuperAI/Hackathon Onsite/Hackathon3/Nano_FormSense_Pipeline"
python3 -m venv .venv
source .venv/bin/activate
pip install -r python/requirements.txt
```

## 3. Calibrate หลังคาดเอว

สวมอุปกรณ์ในตำแหน่งใช้งานจริง ยืนตรงและอยู่นิ่ง `5-10 s`:

```bash
PYTHONPATH=python python3 python/main.py \
  --port /dev/cu.usbmodemXXXX --output-dir data/calibration \
  --session-id waist_standing --label UNLABELED

PYTHONPATH=python python3 python/calibrate.py \
  --stationary data/calibration/waist_standing_raw.csv \
  --out calibration.json
```

ไฟล์ calibration ใช้แก้ `gyro_bias_dps` และหักมุมเอียงที่เกิดจากวิธีติด
อุปกรณ์ (`neutral_pitch_deg`) โดยไม่ต้องใช้ magnetometer.
UNO Q model runner แปลงเครื่องหมาย pitch calibration ให้ตรงกับ convention
ของ model extractor ให้อัตโนมัติ.

## 4. เก็บ GOOD/BAD Training Sessions

Label ต้องมาจาก coach, video annotation หรือ protocol การทดลองที่กำหนดไว้:

```bash
PYTHONPATH=python python3 python/main.py \
  --port /dev/cu.usbmodemXXXX --output-dir data/sessions \
  --session-id runner01_good_01 --label GOOD_FORM \
  --calibration calibration.json
```

บันทึกหลายรอบทั้ง `GOOD_FORM` และ `BAD_FORM` แล้วสร้าง training table:

```bash
PYTHONPATH=python python3 python/build_training_dataset.py \
  --sessions-dir data/sessions --output data/training_features.csv
```

แบ่ง train/test ตาม runner หรือ session ห้ามสุ่ม window จาก run เดียวกันไปทั้ง
สองชุด เพราะจะทำให้ผลประเมินสูงเกินจริง.

## 5. Baseline และการเตือนผู้ใช้

สร้าง baseline จาก session ที่ยืนยันว่าเป็นฟอร์มปกติ:

```bash
PYTHONPATH=python python3 python/build_baseline.py \
  --sessions-dir data/sessions --output data/runner01_baseline.json
```

แจ้งเตือนทันทีพร้อมสรุปทุก 5 นาที:

```bash
PYTHONPATH=python python3 python/main.py \
  --port /dev/cu.usbmodemXXXX --output-dir data/live \
  --session-id runner01_live --calibration calibration.json \
  --baseline data/runner01_baseline.json --alert-mode both --feedback
```

- `realtime`: ตรวจทุก window และ rate-limit การเตือนซ้ำไว้ `20 s`
- `five_min`: สรุป trend ทุก `300 s`
- `both`: ใช้ทั้งสองแบบ
- `--feedback`: ส่ง `@FEAT` และ `@ALERT` ทาง UART ไปยัง buzzer/app gateway ได้

ผลลัพธ์เพิ่ม `runner01_live_alerts.csv` เพื่อใช้ใน dashboard หรือส่งให้ app.
sketch ตัวอย่างรับ `@ALERT`: `ALERT` เปิด `LED_BUILTIN` 1.5 วินาที และ `WARN`
0.4 วินาที; สำหรับ wearable จริงให้เปลี่ยน `FEEDBACK_PIN` ไปยังวงจรขับ
vibration motor หรือ buzzer (ห้ามต่อมอเตอร์ตรงกับขา GPIO).
ข้อความ coaching เป็น wellness feedback ไม่ใช่คำวินิจฉัยทางการแพทย์.

## 6. UNO Q Storage

ส่ง session CSV จากเครื่อง local ไปเก็บบน eMMC ของ UNO Q ผ่าน Wi-Fi/SSH:

```bash
./scripts/sync_dataset_to_unoq.sh data/sessions runner01_good_01 arduino@UNOQ_IP
```

หรือรัน `python/main.py` บน Linux ของ UNO Q แล้วต่อ Nano ผ่าน USB serial.
หลีกเลี่ยงการ stream raw 200 Hz ผ่าน internal MCU Bridge; เก็บทาง USB serial
เข้า Linux หรือส่งไฟล์ CSV มี margin และตรวจ packet loss ได้ง่ายกว่า.

## 7. UNO Q TFLite Inference + Attention

ตัว runner สำหรับโมเดลที่ให้มาอยู่ที่
[python/uno_q_live_inference.py](python/uno_q_live_inference.py) และโหลด
`model/running_form_transformer_fp16.tflite` เป็นค่า default. ตัวนี้เป็น pipeline
แยกสำหรับ **UNO Q Linux MPU**:

```text
Nano 6-axis UART stream
  -> rolling 10 s context, output ทุก 1 s
  -> 5 s biomechanical sliding window
  -> seven model features
  -> LiteRT FP16 model: Good / Bad Form + embedded feature_attention output
  -> *_unoq_features.csv + *_predictions.jsonl + optional @ALERT back to Nano
```

ติดอุปกรณ์ใกล้ sacrum/ด้านหลังเอวให้แน่น และจัดแกนให้ตรงกับ model:
`X = ด้านหน้า`, `Y = ซ้าย`, `Z = ขึ้น`. หากติดบอร์ดคนละทิศ ให้ส่ง
`--body-frame-rotation R00 R01 ... R22` เพื่อหมุนแกนก่อน extraction.
โมเดลนี้ตั้งค่า `200 Hz`; runner จะแจ้ง warning เมื่อ rate จาก timestamp
ไม่ตรงเกิน 5%.

Nano ยังส่ง raw samples ต่อเนื่องที่ `200 Hz`; คำว่า window `5 s` หมายถึง
UNO Q สะสมข้อมูล 5 วินาทีเพื่อสกัด feature แล้วอัปเดตทุก 1 วินาที ไม่ใช่ให้
Nano หยุดส่งแล้วส่ง batch ทุก 5 วินาที.

ติดตั้ง runtime บน UNO Q:

```bash
pip install -r python/requirements_unoq.txt
```

ใช้งานกับ Nano ที่ต่อ USB serial เข้ากับ UNO Q:

```bash
PYTHONPATH=python python3 python/uno_q_live_inference.py \
  --port /dev/ttyACM0 --baud 460800 \
  --model model/running_form_transformer_fp16.tflite \
  --calibration calibration.json \
  --enable-metric-alerts \
  --ble-address "AA:BB:CC:DD:EE:FF" \
  --ble-characteristic "0000fff1-0000-1000-8000-00805f9b34fb" \
  --output-dir ~/formsense_data/live
```

เมื่อ export normalizer จาก training run เดิมได้แล้ว ให้เพิ่ม:

```bash
--normalizer model/running_form_normalizer.json
```

ใช้ replay CSV ทดสอบโดยไม่ต่อ Nano:

```bash
PYTHONPATH=python python3 python/uno_q_live_inference.py \
  --replay-csv data/demo/bad01_raw.csv \
  --model model/running_form_transformer_fp16.tflite \
  --output-dir data/unoq_demo
```

ผล JSON ต่อ window สำหรับ dashboard/app มีโครงแบบนี้:

```json
{
  "type": "running_form_prediction",
  "window_id": 1,
  "features": {"cadence_spm": 168.0, "trunk_forward_lean_deg": 12.4},
  "class": "Bad Form",
  "probabilities": {"Good": 0.2, "Bad Form": 0.8},
  "attention_weights": {"cadence_spm": 0.18, "trunk_forward_lean_deg": 0.24},
  "dominant_feature": "trunk_forward_lean_deg",
  "coaching_cue": "Forward lean is influential; reset torso alignment from the ankles.",
  "feedback": {"bad_form_detected": true, "alert_triggered": true}
}
```

### Metric Triggers & Priority System

กฎด้านล่างเป็น **rule-based coaching layer** ไม่ใช่ข้อความที่โมเดล TFLite
เรียนมาเอง. โมเดลให้ `Good/Bad Form` กับ `feature_attention`; trigger ใช้ค่า
ที่สกัดได้เพื่อเลือกข้อความที่เข้าใจง่ายและจัดลำดับ alert.

| Priority | Trigger | Severity | ข้อความใน dashboard |
| ---: | --- | --- | --- |
| 1 | estimated peak impact `> 2.5 BW` | `ALERT` | แรงกระแทกประมาณการสูงมาก ลองลงเท้าให้นุ่มและใต้ลำตัวมากขึ้น |
| 2 | estimated peak impact `> 2.0 BW` | `WARN` | แรงกระแทกประมาณการเริ่มสูง ลองลดการก้าวยื่นและลงเบาขึ้น |
| 2 | asymmetry proxy `> 10%` | `WARN` | รูปแบบซ้าย-ขวาไม่สมดุล เช็คว่าสายคาดแน่นตรงกลางและปรับจังหวะก้าว |
| 3 | estimated `GCT > 300 ms` | `WARN` | เท้าแตะพื้นนานขึ้น ลองก้าวให้เบาและคืนเท้าไวขึ้น |
| 3 | forward lean `> 15 deg` | `WARN` | ลำตัวเอนไปข้างหน้ามาก ลองจัดแนวลำตัวใหม่จากข้อเท้า |
| 4 | vertical oscillation `> 10 cm` | `WARN` | ตัวเด้งมากขึ้น ลองส่งแรงไปข้างหน้าและก้าวให้เงียบลง |
| 5 | cadence `< 160` หรือ `> 200 spm` | `INFO` | แนะนำปรับจังหวะเล็กน้อย โดยต้องดู pace ของผู้วิ่งด้วย |
| 6 | foot-strike time-to-peak `< 15 ms` | `INFO` | สัญญาณลงเท้าคม ลองลดการก้าวยื่นและลงเท้านุ่มขึ้น |

จุดที่ปรับจากข้อความเริ่มต้น:

- ไม่ใช้คำว่า “อันตราย” กับ vGRF เพราะค่าจาก sensor เอวเป็น
  `peak_vgrf_bw_estimate` ไม่ใช่ force plate measurement.
- ไม่แจ้งว่า oscillation `< 4 cm` หรือ lean `< 3 deg` เป็นปัญหาโดยอัตโนมัติ
  เพราะขึ้นกับ pace, anatomy และ baseline ส่วนบุคคล.
- ไม่สั่งให้เปลี่ยนเป็น midfoot strike จาก waist IMU เพียงตัวเดียว; ให้แนะนำ
  ลด overstride/ลงนุ่มขึ้น และ validate ด้วย video หรือ foot sensor ก่อน.
- `INFO` แสดงบน app ได้ แต่ไม่ pulse vibration; UART alert ใช้เฉพาะ
  `WARN`/`ALERT` ที่เปิดด้วย `--enable-metric-alerts`.

`metric_triggers` ใน JSONL เก็บข้อความภาษาไทยและ evidence note ส่วน packet
UART กลับ Nano ใช้ข้อความ ASCII สั้นเพื่อให้ protocol/CRC เสถียร.

### Bluetooth Delivery and Offline Storage

UNO Q มี Bluetooth 5.1. Runner ใช้ BLE สำหรับส่งเฉพาะ `WARN`/`ALERT` ไปยัง
device ปลายทาง ไม่ส่ง raw stream. ปลายทางต้องเป็น BLE peripheral ที่มี
**writable GATT characteristic**; ตั้งค่าด้วย `--ble-address` และ
`--ble-characteristic`.

เมื่อ BLE พบ device และเขียน characteristic ได้:

```json
{"type":"formsense_alert","ts":10.0,"severity":"WARN","code":"asymmetry_high"}
```

เมื่อ BLE ไม่พบ, disconnect, หรือไม่ได้กำหนด BLE target:

```text
<output-dir>/<session-id>_ble_outbox.jsonl     alerts ที่รอส่ง
<output-dir>/<session-id>_ble_delivery.jsonl   audit log การส่ง/เก็บ/retry
```

Runner จะ retry pending outbox เมื่อเริ่มโปรแกรมครั้งถัดไปด้วย BLE config
เดิม และหลังส่ง alert ใหม่ผ่าน Bluetooth สำเร็จ. หากต้องการส่ง feedback กลับ
Nano ทางสาย serial พร้อมกัน ให้เพิ่ม `--uart-feedback`.

ตรวจแล้วไฟล์ `model/running_form_transformer_fp16.tflite` มี input
`features[1,7]` และ outputs `feature_attention[1,7]`, `probabilities[1,2]`.
ข้อสำคัญ: โมเดลนี้ยัง **ไม่มี normalizer จาก training split** รวมอยู่ในการ
ส่งมอบ. การรันโดยไม่
ส่ง `--normalizer` จะยังสร้าง JSON/attention สำหรับ integration demo แต่ระบุ
`production_ready=false` และไม่ส่ง alert ที่อาศัย model probability อัตโนมัติ.
การส่ง `--enable-metric-alerts` เปิด rule-based alerts แยกต่างหากโดยตั้งใจ
สำหรับ prototype ซึ่งต้อง validate thresholds กับข้อมูลจริงก่อน. ต้อง export
`running_form_normalizer.json` จากการ train ชุดเดียวกันก่อนใช้ผล Good/Bad
แจ้งผู้วิ่งจริง. Attention weight แปลว่า feature ที่โมเดลให้น้ำหนักใน window
นั้น ไม่ใช่หลักฐานเชิงสาเหตุหรือการวินิจฉัยการบาดเจ็บ.

## Test without Hardware

```bash
PYTHONPATH=python python3 python/simulate_session.py \
  --output-dir data/demo --session-id good01 --good-form
PYTHONPATH=python python3 python/build_baseline.py \
  --sessions-dir data/demo --output data/demo/baseline.json
PYTHONPATH=python python3 python/simulate_session.py \
  --output-dir data/demo --session-id bad01 \
  --baseline data/demo/baseline.json --alert-mode both --summary-interval-s 5
python3 -m unittest discover -s tests -v
```

## Sources

- [Arduino_BMI270_BMM150 Library](https://docs.arduino.cc/libraries/arduino_bmi270_bmm150)
- [Nano 33 BLE Sense Rev2 IMU Guide](https://docs.arduino.cc/tutorials/nano-33-ble-sense-rev2/cheat-sheet)
- [UNO Q User Manual](https://docs.arduino.cc/tutorials/uno-q/user-manual/)
- [UNO Q Datasheet](https://docs.arduino.cc/resources/datasheets/ABX00162-datasheet.pdf)
- [UNO Q Bridge API](https://docs.arduino.cc/software/app-lab/bridge/bridge-api/)
- [Google AI Edge LiteRT Python Inference](https://ai.google.dev/edge/litert/inference)
- [Validation of a Torso-Mounted Accelerometer for VO and GCT During Treadmill Running (PubMed)](https://pubmed.ncbi.nlm.nih.gov/26695636/)
- [Wearable Sensor-Based Real-Time Gait Detection: A Systematic Review (Sensors, 2021)](https://doi.org/10.3390/s21082727)
