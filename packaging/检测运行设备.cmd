@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONHOME=
set PYTHONPATH=
cd /d "%~dp0"
"runtime\python.exe" device_check.py
pause
