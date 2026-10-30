@echo off
rem Job Manager tools: host PC check, serial and database check, backups, database upkeep.
cd /d "%~dp0"
title Job Manager tools
python --version >nul 2>&1
if errorlevel 1 (
    echo Python is not installed or not on PATH. Run run.bat first.
    pause
    exit /b 1
)
python jm_tools.py %*
if not "%~1"=="" pause
