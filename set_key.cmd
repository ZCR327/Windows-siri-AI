@echo off
rem Double-click this file to set the LLM api key (nothing is sent to chat).
setlocal
cd /d "%~dp0"
set PY=
where python >nul 2>nul && set PY=python
if "%PY%"=="" (
  echo [FAIL] python not found in PATH
  pause
  exit /b 1
)
"%PY%" set_key.py %*
echo.
pause
