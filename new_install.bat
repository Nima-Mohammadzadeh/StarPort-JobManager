@echo off
title Job Manager - new install
cd /d "%~dp0"
echo This makes a NEW, EMPTY Job Manager database in this folder.
echo Only do this for a brand-new install. If you are updating, close this window:
echo copy the new files over your existing Job Manager folder instead.
echo.
choice /c YN /m "Create a new, empty database here"
if errorlevel 2 exit /b 0
python -c "import flask, openpyxl, waitress, segno" >nul 2>&1
if errorlevel 1 (
    echo Installing Flask, openpyxl, waitress and segno...
    python -m pip install -r requirements.txt --quiet
)
python app.py --create-database
pause
