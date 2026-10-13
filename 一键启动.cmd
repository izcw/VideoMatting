@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" goto missing
".venv\Scripts\python.exe" launcher.py %*
if errorlevel 1 goto failed
echo.
echo 启动窗口已结束。
exit /b 0
:missing
echo Python 环境缺失，请先运行「安装环境.cmd」。
goto failed
:failed
echo 启动失败，请查看上方提示或 data\server-error.log。
pause
exit /b 1
