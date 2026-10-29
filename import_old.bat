@echo off
title Job Manager - import from old app
cd /d "%~dp0"
if "%~1"=="" (
    echo Drag the OLD Job Manager folder onto this file,
    echo or run:  import_old.bat "C:\path\to\db-app"
    pause
    exit /b 1
)
echo Make sure Job Manager is NOT running before you continue.
echo.
echo Which jobs should come over?
echo   1 = all jobs (open and archived)
echo   2 = only open jobs
echo   3 = no jobs, just serial numbers and templates
choice /c 123 /n /m "Choose 1, 2 or 3: "
if errorlevel 3 (set J=none) else if errorlevel 2 (set J=open) else (set J=all)
echo.
echo --- Preview ---
python import_old.py "%~1" --jobs %J% --dry-run
echo.
choice /c YN /m "Apply this import"
if errorlevel 2 exit /b 0
python import_old.py "%~1" --jobs %J%
pause
