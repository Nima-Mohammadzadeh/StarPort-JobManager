#!/usr/bin/env bash
cd "$(dirname "$0")"
python3 -c "import flask, openpyxl, waitress, segno" 2>/dev/null || python3 -m pip install --user -r requirements.txt
exec python3 app.py
