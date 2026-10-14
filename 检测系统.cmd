@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONHOME=
set PYTHONPATH=
cd /d "%~dp0"
title 清影工坊 - 检测系统
if exist "runtime\python.exe" goto portable
if exist ".venv\Scripts\python.exe" goto source
python device_check.py
goto finished
:portable
"runtime\python.exe" device_check.py
goto finished
:source
".venv\Scripts\python.exe" device_check.py
:finished
echo 按任意键关闭窗口...
pause >nul
