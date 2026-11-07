@echo off
cd /d "%~dp0"
python settings.py
if errorlevel 1 pause
