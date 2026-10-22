@echo off
title TataElexy Test Suite
cd /d "%~dp0"
echo ========================================================
echo   Running TataElexy Adaptive QoS Test Suite
echo ========================================================
echo.

set "PY=C:\Users\akshat\AppData\Local\Programs\Python\Python311\python.exe"
if exist "%PY%" (
    "%PY%" -m pytest -v tests/
) else (
    python -m pytest -v tests/
)

echo.
pause
