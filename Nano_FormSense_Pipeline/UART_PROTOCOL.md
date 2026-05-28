# Nano FormSense Waist Sensor UART Protocol

## Recommended Capture Path

```text
Arduino Nano + accelerometer/gyroscope -- USB Serial / 3.3 V UART @ 460800 --> Collector
Collector = laptop local storage OR UNO Q Linux/eMMC storage
```

The Nano sends raw measurements only. Filtering and features run in Python so
raw values remain available when filter parameters or labels change during
training preparation.

## UART Wiring

For USB-capable Nano boards, use their USB serial connection. For pin UART:

| Nano / UART adapter | Collector receiver | Notes |
| --- | --- | --- |
| TX | RX | Raw IMU packets |
| RX | TX | Optional ACK / feature feedback |
| GND | GND | Common reference |

Use voltage levels compatible with the selected Nano and receiver. If receiving
on UNO Q maker I/O, use **3.3 V logic only**; UNO Q maker pins are not 5 V input
tolerant for this design.

## Settings

- Baud rate recommended: `460800`
- Target sample rate: `200 Hz` when the IMU driver supports that ODR
- Format: `8-N-1`, newline-terminated ASCII
- Integrity: CRC-16/CCITT-FALSE over all characters before `*`, initial `0xFFFF`

The six-axis packet fits comfortably at `460800`. Verify the actual sensor ODR
using timestamps and confirm sequence numbers have no gaps before collecting
training sessions. If an IMU library exposes only about `100 Hz`, store that
rate in dataset metadata and do not describe the session as `200 Hz`.

## Raw IMU Packet: Nano to Collector

```text
@IMU,seq,timestamp_s,acc_x_g,acc_y_g,acc_z_g,gyro_x_dps,gyro_y_dps,gyro_z_dps*CRC16
```

Example format:

```text
@IMU,42,4.2000,0.01200,-0.02300,1.08300,0.1200,-1.4300,0.0800*ABCD
```

## Optional Feedback: Collector to Nano or Receiver

When `python/main.py` is started with `--feedback`, it returns:

```text
@ACK,seq*CRC16
@FEAT,window_id,end_time_s,cadence_spm,vertical_oscillation_cm,gct_flight_balance_ms,impact_loading_rate_bw_s,trunk_forward_lean_deg,left_right_asymmetry_pct,heel_strike_likelihood,form_label*CRC16
@ALERT,severity,end_time_s,message*CRC16
```

The full dataset is stored as CSV; `@FEAT` is intended for a live display or
downstream integration, not as the only training record. `@ALERT` is produced
only when a personal baseline and alert mode are enabled.
