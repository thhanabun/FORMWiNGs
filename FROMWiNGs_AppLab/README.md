# FROMWiNGs App Lab Package

Flow:

```text
Nano TX/D1 -> UNO Q RX/D0 MCU -> RouterBridge -> UNO Q Linux model
UNO Q Linux model -> RouterBridge -> UNO Q MCU -> BLE Notify FROMWiNGs
```

Wiring:

```text
Nano TX/D1 -> UNO Q RX/D0
Nano GND   -> UNO Q GND
Modulino Thermo -> UNO Q I2C connector
```

App Lab files:

```text
sketch/sketch.ino  MCU bridge + BLE advertiser
python/main.py     Linux entrypoint, runs rule-based form classifier + thermal context
python/            Model pipeline code
model/             Rule-base metadata, legacy XGBoost assets, normalizer, and TFLite fallback
libraries/         Arduino sketch libraries required by App Lab compile
```

BLE:

```text
Name: FROMWiNGs
Service: 19B10000-E8F2-537E-4F6C-D104768A1214
Characteristic: 19B10001-E8F2-537E-4F6C-D104768A1214
Properties: Read, Notify
```

Nano still needs to be programmed once from this workspace. This package uses
the Nano 33 BLE Sense `Arduino_LSM9DS1` IMU library:

```powershell
.\program_formsense_nano.bat COM8
```

The UNO Q MCU bridge can also be programmed from this workspace without using
the App Lab editor:

```powershell
.\program_formsense_unoq.bat COM6
```

To program both boards from the command line:

```powershell
.\program_formsense_all.bat COM6 COM8
```

Keep running this `FROMWiNGs_AppLab` app in Arduino App Lab for the UNO Q Linux
Python model process. The batch files above program the board firmware; the
App Lab package still owns the Linux-side `python/main.py` app.

If App Lab reports `ArduinoBLE.h: No such file or directory`, make sure the
whole `libraries/` folder is included in the App Lab app. It contains:

```text
ArduinoBLE
Arduino_SpiNINA
Arduino_RouterBridge
Arduino_RPClite
MsgPack
ArxContainer
ArxTypeTraits
DebugLog
```
