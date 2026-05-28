@echo off
setlocal EnableExtensions

set "ROOT=%~dp0"
set "CLI=%ROOT%.tools\arduino-cli\arduino-cli.exe"
set "UNOQ_PORT=%~1"
set "UNOQ_FQBN=arduino:zephyr:unoq:link_mode=dynamic,wait_linux_boot=yes"
set "UNOQ_APP=%ROOT%FROMWiNGs_AppLab"
set "UNOQ_SKETCH=%UNOQ_APP%\sketch"
set "UNOQ_LIBRARIES=%UNOQ_APP%\libraries"

if "%UNOQ_PORT%"=="" set "UNOQ_PORT=COM6"

if not exist "%CLI%" (
  echo ERROR: Arduino CLI not found:
  echo %CLI%
  echo Run install_arduino_test_tools.bat first.
  exit /b 1
)

echo === FormSense UNO Q programmer ===
echo Port     : %UNOQ_PORT%
echo FQBN     : %UNOQ_FQBN%
echo App dir  : %UNOQ_APP%
echo Sketch   : %UNOQ_SKETCH%
echo Libraries: %UNOQ_LIBRARIES%
echo.

echo Compiling UNO Q bridge sketch...
"%CLI%" compile --fqbn %UNOQ_FQBN% --libraries "%UNOQ_LIBRARIES%" "%UNOQ_SKETCH%"
if errorlevel 1 goto :failed

echo.
echo Uploading UNO Q bridge sketch to %UNOQ_PORT%...
"%CLI%" upload -p %UNOQ_PORT% --fqbn %UNOQ_FQBN% "%UNOQ_SKETCH%"
if errorlevel 1 goto :failed

echo.
echo Done. UNO Q MCU now runs the Nano UART -> RouterBridge -> BLE bridge.
echo Keep the FROMWiNGs_AppLab Python app running on UNO Q Linux for model output.
exit /b 0

:failed
echo.
echo ERROR: UNO Q programming failed.
echo Close Arduino App Lab serial monitors or anything using %UNOQ_PORT%, then run again.
exit /b 1
