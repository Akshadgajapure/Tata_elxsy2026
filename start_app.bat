@echo off
title TataElexy Adaptive QoS Engine
cd /d "%~dp0"
echo ========================================================
echo   TataElexy Adaptive QoS Engine
echo ========================================================
echo.
echo Activating local virtual environment if present...
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"
echo Starting Flask dashboard at http://127.0.0.1:5000 ...
echo Press Ctrl+C to stop the server.
echo.
python app.py
if %ERRORLEVEL% NEQ 0 (
  echo.
  echo Server stopped with error code %ERRORLEVEL%.
  pause
)
