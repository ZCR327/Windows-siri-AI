@echo off
rem Double-click this file, then speak Chinese for 10 seconds.
setlocal
cd /d "%~dp0"
set PY=
where python >nul 2>nul && set PY=python
if "%PY%"=="" (
  echo [FAIL] python not found in PATH
  pause
  exit /b 1
)
"%PY%" mic_test.py %*
echo.
pause
