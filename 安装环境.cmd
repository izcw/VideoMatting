@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" python -m venv .venv
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install -r requirements.lock.txt
if errorlevel 1 goto fail
".venv\Scripts\python.exe" bootstrap.py
if errorlevel 1 goto fail
call pnpm --dir web install --frozen-lockfile
if errorlevel 1 goto fail
call pnpm --dir web build
if errorlevel 1 goto fail
echo 安装完成，请双击一键启动.cmd。
pause
exit /b 0
:fail
echo 安装失败，请检查上方错误。需要 Python 3.13、Node.js 和 pnpm。
pause
exit /b 1
