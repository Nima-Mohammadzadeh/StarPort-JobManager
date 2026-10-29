@echo off
title Job Manager - import saved proofs
cd /d "%~dp0"
if "%~1"=="" (
    echo Drag the OLD RFID Proofing Utility folder ^(the one with START_HERE.bat^) onto this file,
    echo or run:  import_proofing.bat "C:\path\to\Proofing Test"
    pause
    exit /b 1
)
echo Make sure Job Manager is NOT running before you continue.
echo.
echo --- Preview ---
python import_proofing.py "%~1" --dry-run
echo.
choice /c YN /m "Apply this import"
if errorlevel 2 exit /b 0
python import_proofing.py "%~1"
pause
