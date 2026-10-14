@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
set PYTHONHOME=
set PYTHONPATH=
if exist "runtime\python.exe" goto portable
if not exist ".venv\Scripts\python.exe" (
  echo 尚未准备运行环境，没有可停止的服务。
  echo 按任意键关闭窗口...
  pause >nul
  exit /b 0
)
".venv\Scripts\python.exe" launcher.py --stop
goto finished
:portable
"runtime\python.exe" launcher.py --stop
:finished
echo 按任意键关闭窗口...
pause >nul
