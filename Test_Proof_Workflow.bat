@echo off
cd /d "%~dp0"
python -c "import flask, openpyxl" >nul 2>&1
if errorlevel 1 (
  echo Required Job Manager dependencies are missing. Use the normal Job Manager installation first.
  pause
  exit /b 1
)
python tests\test_proof_service.py
set "result=%errorlevel%"
echo.
if not "%result%"=="0" (echo Proof tests FAILED. Do not deploy.) else (echo Proof tests completed. Check for skipped tests before deploying.)
pause
exit /b %result%
