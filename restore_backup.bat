@echo off
title Job Manager - restore from a backup
cd /d "%~dp0"
if "%~1"=="" (
    echo Drag a backup zip onto this file,
    echo or run:  restore_backup.bat "Z:\...\Backups\backup_2026-10-29_06-00-00.zip"
    echo.
    echo This is for emergencies only. See RESTORE.md first.
    pause
    exit /b 1
)
echo Close the Job Manager window before you continue.
echo.
echo --- Preview (nothing is changed) ---
python restore_backup.py "%~1"
if errorlevel 1 (
    pause
    exit /b 1
)
echo.
choice /c YN /m "Restore from this backup"
if errorlevel 2 exit /b 0
python restore_backup.py "%~1" --apply
pause
