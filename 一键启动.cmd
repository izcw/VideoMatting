@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
set PYTHONHOME=
set PYTHONPATH=
title 清影工坊 1.0.0
if exist "runtime\python.exe" goto portable
if not exist ".venv\Scripts\python.exe" goto setup
".venv\Scripts\python.exe" setup_environment.py --check
if errorlevel 1 goto setup
:source
".venv\Scripts\python.exe" launcher.py %*
goto finished
:portable
"runtime\python.exe" launcher.py %*
goto finished
:setup
echo 首次使用，正在准备运行环境和模型，请保持联网...
python setup_environment.py
if errorlevel 1 goto failed
goto source
:finished
if errorlevel 1 goto failed
echo.
echo 启动窗口已结束。
exit /b 0
:failed
echo 启动失败，请查看上方提示或 data\server-error.log。
echo 按任意键关闭窗口...
pause >nul
exit /b 1
