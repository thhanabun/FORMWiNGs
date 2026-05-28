@echo off
setlocal EnableExtensions

set "ROOT=%~dp0"
set "CLI=%ROOT%.tools\arduino-cli\arduino-cli.exe"
set "UNOQ_PORT=%~1"
set "NANO_PORT=%~2"

if "%UNOQ_PORT%"=="" set "UNOQ_PORT=COM6"
if "%NANO_PORT%"=="" set "NANO_PORT=COM8"

echo.
echo === Stop UNO Q + Nano UART demo ===
echo UNO Q port : %UNOQ_PORT%
echo Nano port  : %NANO_PORT%
echo.

if not exist "%CLI%" (
  echo ERROR: Arduino CLI not found:
  echo %CLI%
  echo Run install_arduino_test_tools.bat first.
  exit /b 1
)

echo Compiling Nano stop sketch...
"%CLI%" compile --fqbn arduino:mbed_nano:nano33ble "%ROOT%nano_stop_sender"
if errorlevel 1 goto :failed

echo Uploading Nano stop sketch...
"%CLI%" upload -p %NANO_PORT% --fqbn arduino:mbed_nano:nano33ble "%ROOT%nano_stop_sender"
if errorlevel 1 goto :failed

echo.
echo Compiling UNO Q stop sketch...
"%CLI%" compile --fqbn arduino:zephyr:unoq "%ROOT%uno_q_stop_receiver"
if errorlevel 1 goto :failed

echo Uploading UNO Q stop sketch...
"%CLI%" upload -p %UNOQ_PORT% --fqbn arduino:zephyr:unoq "%ROOT%uno_q_stop_receiver"
if errorlevel 1 goto :failed

echo.
echo === Done ===
echo Nano stopped sending UART telemetry.
echo UNO Q receiver demo stopped.
echo.
exit /b 0

:failed
echo.
echo ERROR: Stop script failed.
echo If App Lab monitor is open, press Ctrl+C or close Serial Monitor, then run again.
exit /b 1

