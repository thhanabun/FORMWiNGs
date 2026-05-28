@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "ROOT=%~dp0"
set "TOOLS_DIR=%ROOT%.tools"
set "CLI_DIR=%TOOLS_DIR%\arduino-cli"
set "CLI_EXE=%CLI_DIR%\arduino-cli.exe"
set "CLI_ZIP=%TOOLS_DIR%\arduino-cli.zip"
set "CLI_URL=https://downloads.arduino.cc/arduino-cli/arduino-cli_latest_Windows_64bit.zip"
set "NANO_SKETCH=%ROOT%nano_real_signal_uart"
set "UNOQ_SKETCH=%ROOT%uno_q_real_signal_receiver"

echo.
echo === Arduino test tools installer ===
echo Workspace: %ROOT%
echo.

if not exist "%TOOLS_DIR%" mkdir "%TOOLS_DIR%"
if not exist "%CLI_DIR%" mkdir "%CLI_DIR%"

if not exist "%CLI_EXE%" (
  echo Downloading Arduino CLI...
  curl.exe -L --fail --output "%CLI_ZIP%" "%CLI_URL%"
  if errorlevel 1 goto :download_failed

  echo Extracting Arduino CLI...
  tar.exe -xf "%CLI_ZIP%" -C "%CLI_DIR%"
  if errorlevel 1 goto :extract_failed
) else (
  echo Arduino CLI already exists: %CLI_EXE%
)

echo.
echo Arduino CLI version:
"%CLI_EXE%" version
if errorlevel 1 goto :cli_failed

echo.
echo Initializing Arduino CLI config if needed...
"%CLI_EXE%" config init --overwrite
if errorlevel 1 goto :cli_failed

echo.
echo Updating package and library indexes...
"%CLI_EXE%" core update-index
if errorlevel 1 goto :cli_failed
"%CLI_EXE%" lib update-index
if errorlevel 1 goto :cli_failed

echo.
echo Installing board cores...
"%CLI_EXE%" core install arduino:avr
if errorlevel 1 goto :cli_failed
"%CLI_EXE%" core install arduino:megaavr
if errorlevel 1 goto :cli_failed
"%CLI_EXE%" core install arduino:mbed_nano
if errorlevel 1 goto :cli_failed
"%CLI_EXE%" core install arduino:zephyr
if errorlevel 1 goto :cli_failed

echo.
echo Installing required libraries...
"%CLI_EXE%" lib install Arduino_RouterBridge
if errorlevel 1 goto :cli_failed
"%CLI_EXE%" lib install Arduino_LSM9DS1
if errorlevel 1 goto :cli_failed

echo.
echo Detecting UNO Q FQBN...
set "UNOQ_FQBN="
"%CLI_EXE%" board listall "UNO Q" | findstr /C:"arduino:zephyr:unoq" >nul
if not errorlevel 1 set "UNOQ_FQBN=arduino:zephyr:unoq"

if "%UNOQ_FQBN%"=="" (
  "%CLI_EXE%" board listall "UNO Q" | findstr /C:"arduino:zephyr:uno_q" >nul
  if not errorlevel 1 set "UNOQ_FQBN=arduino:zephyr:uno_q"
)

if "%UNOQ_FQBN%"=="" (
  echo Could not auto-detect UNO Q FQBN. Available UNO Q entries:
  "%CLI_EXE%" board listall "UNO"
  goto :unoq_failed
)

echo UNO Q FQBN: %UNOQ_FQBN%

echo.
echo Compiling Nano sender for Nano 33 BLE Sense...
"%CLI_EXE%" compile --fqbn arduino:mbed_nano:nano33ble "%NANO_SKETCH%"
if errorlevel 1 goto :compile_failed

echo.
echo Compiling UNO Q receiver...
"%CLI_EXE%" compile --fqbn %UNOQ_FQBN% "%UNOQ_SKETCH%"
if errorlevel 1 goto :compile_failed

echo.
echo === Done ===
echo Arduino CLI is installed locally at:
echo %CLI_EXE%
echo.
echo To compile again:
echo "%CLI_EXE%" compile --fqbn arduino:mbed_nano:nano33ble "%NANO_SKETCH%"
echo "%CLI_EXE%" compile --fqbn %UNOQ_FQBN% "%UNOQ_SKETCH%"
echo.
exit /b 0

:download_failed
echo.
echo ERROR: Could not download Arduino CLI.
echo Check internet access, then run this file again.
exit /b 1

:extract_failed
echo.
echo ERROR: Could not extract Arduino CLI zip.
exit /b 1

:cli_failed
echo.
echo ERROR: Arduino CLI command failed.
exit /b 1

:compile_failed
echo.
echo ERROR: Compile failed. Read the error above and fix the sketch or board package.
exit /b 1

:unoq_failed
echo.
echo ERROR: UNO Q board package installed, but the FQBN name was not found.
echo Try opening AppLab/Arduino IDE and updating Arduino Zephyr Boards.
exit /b 1
