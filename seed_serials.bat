@echo off
title Job Manager - find serials already used
cd /d "%~dp0"
if "%~1"=="" (
    echo Drag the folder that holds your old job folders onto this file,
    echo or run:  seed_serials.bat "Z:\3 Encoding and Printing Files\Customers Encoding Files"
    pause
    exit /b 1
)
echo.
echo --- Preview (nothing is written) ---
python seed_serials.py "%~1"
echo.
choice /c YN /m "Record these in the serial registry"
if errorlevel 2 exit /b 0
python seed_serials.py "%~1" --apply
pause
