@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONHOME=
set PYTHONPATH=
cd /d "%~dp0"
title 清影工坊 1.0.0
echo 清影工坊绿色版 1.0.0 - 正在启动，请稍候...
echo 此版本使用本机 CPU 处理，视频不会上传到云端。
"runtime\python.exe" launcher.py %*
if errorlevel 1 (
  echo 启动失败，请查看上方提示或 data\server-error.log。
  pause
)
