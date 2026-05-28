# FormSense / FROMWiNGs Hardware Bridge

This workspace connects a Nano 33 BLE Sense IMU sender to an Arduino UNO Q App Lab
pipeline.

## Current Flow

```text
Nano 33 BLE Sense
  -> UART CSV on D1/TX
UNO Q MCU sketch
  -> RouterBridge batches
UNO Q Linux Python app
  -> feature extraction + TFLite model
UNO Q MCU sketch
  -> BLE notify as FROMWiNGs
```

## Wiring

```text
Nano TX / D1 -> UNO Q RX / D0
Nano GND     -> UNO Q GND
```

## Program Boards

Program both boards:

```powershell
.\program_formsense_all.bat COM6 COM8
```

Or separately:

```powershell
.\program_formsense_nano.bat COM8
.\program_formsense_unoq.bat COM6
```

Typical ports in this setup:

```text
UNO Q : COM6
Nano  : COM8
```

## Run App Lab

Open `FROMWiNGs_AppLab` in Arduino App Lab and press `Run`.

The App Lab Python side must keep running because it performs model inference.
The batch files only program board firmware.

## What Was Fixed

- Nano sender now uses the Nano 33 BLE Sense `Arduino_LSM9DS1` IMU library.
- Nano sends filtered CSV at 50 Hz over `Serial1` / UART.
- UNO Q sketch receives Nano UART, batches IMU lines, and exposes
  `formsense/pop_imu_batch` to Python.
- Python polls RouterBridge, parses IMU lines, runs feature extraction and model
  inference, and logs `FULL_PAYLOAD_JSON`.
- Feature extraction now resamples dropped/sparse packets back to a uniform 50 Hz
  timeline.
- A fallback feature window emits predictions even when step detection is not
  confident yet.
- BLE payloads are chunked with `ble_begin`, `ble_chunk`, and `ble_commit` so
  large prediction JSON can pass through RouterBridge safely.

## Healthy Logs

You should see:

```text
FULL_PAYLOAD_JSON={...}
"mcu_ble":{"status":"SENT_TO_MCU","bytes":...,"chunks":...}
```

This means the full route is working:

```text
Nano sensor -> UNO Q MCU -> Python model -> UNO Q MCU -> BLE
```

## Remaining Tuning

If payloads contain:

```json
"fallback_window": 1.0,
"detected_step_events": 0.0
```

the system is working, but the biomechanics extractor is still using fallback
features. Next tuning work should focus on sensor placement, neutral calibration,
and step-event thresholds.
