@echo off
setlocal EnableExtensions

set "ROOT=%~dp0"
set "UNOQ_PORT=%~1"
set "NANO_PORT=%~2"

if "%UNOQ_PORT%"=="" set "UNOQ_PORT=COM6"
if "%NANO_PORT%"=="" set "NANO_PORT=COM8"

echo.
echo === FormSense full programmer ===
echo UNO Q port : %UNOQ_PORT%
echo Nano port  : %NANO_PORT%
echo.
echo This programs:
echo   1. Nano sender from FROMWiNGs_AppLab\nano_sender
echo   2. UNO Q bridge from FROMWiNGs_AppLab\sketch
echo.

call "%ROOT%program_formsense_nano.bat" %NANO_PORT%
if errorlevel 1 goto :failed

echo.
call "%ROOT%program_formsense_unoq.bat" %UNOQ_PORT%
if errorlevel 1 goto :failed

echo.
echo === Done ===
echo Hardware wiring must still be:
echo   Nano TX/D1 -^> UNO Q RX/D0
echo   Nano GND   -^> UNO Q GND
echo.
echo Start or keep running FROMWiNGs_AppLab in Arduino App Lab for the Linux model app.
exit /b 0

:failed
echo.
echo ERROR: Full programming failed.
echo Close Arduino App Lab serial monitors or anything using %UNOQ_PORT% / %NANO_PORT%, then run again.
exit /b 1
