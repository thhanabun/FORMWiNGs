@echo off
setlocal EnableExtensions

set "ROOT=%~dp0"
set "CLI=%ROOT%.tools\arduino-cli\arduino-cli.exe"
set "NANO_PORT=%~1"
set "NANO_FQBN=arduino:mbed_nano:nano33ble"
set "NANO_SKETCH=%ROOT%FROMWiNGs_AppLab\nano_sender"

if "%NANO_PORT%"=="" set "NANO_PORT=COM8"

if not exist "%CLI%" (
  echo ERROR: Arduino CLI not found:
  echo %CLI%
  echo Run install_arduino_test_tools.bat first.
  exit /b 1
)

echo === FormSense Nano programmer ===
echo Port  : %NANO_PORT%
echo FQBN  : %NANO_FQBN%
echo Sketch: %NANO_SKETCH%
echo.

echo Installing Nano IMU library if needed...
"%CLI%" lib install Arduino_LSM9DS1
if errorlevel 1 goto :failed

echo.
echo Compiling Nano filtered CSV sender...
"%CLI%" compile --fqbn %NANO_FQBN% "%NANO_SKETCH%"
if errorlevel 1 goto :failed

echo.
echo Uploading Nano filtered CSV sender to %NANO_PORT%...
"%CLI%" upload -p %NANO_PORT% --fqbn %NANO_FQBN% "%NANO_SKETCH%"
if errorlevel 1 goto :failed

echo.
echo Done. Nano now sends filtered CSV on Serial1 at 115200 baud, target 50 Hz:
echo seq,timestamp_s,acc_x_g,acc_y_g,acc_z_g,gyro_x_dps,gyro_y_dps,gyro_z_dps
exit /b 0

:failed
echo.
echo ERROR: Nano programming failed. Check the port and board package/library output above.
exit /b 1
