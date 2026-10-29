@echo off
title Job Manager
cd /d "%~dp0"
python --version >nul 2>&1
if errorlevel 1 (
    echo Python is not installed or not on PATH. Install Python 3.10+ from python.org
    echo and tick "Add Python to PATH" during setup.
    pause
    exit /b 1
)
python -c "import flask, openpyxl, waitress, segno" >nul 2>&1
if errorlevel 1 (
    echo Installing Flask, openpyxl, waitress and segno...
    python -m pip install -r requirements.txt --quiet
)
python app.py
echo.
echo Job Manager stopped.
pause
