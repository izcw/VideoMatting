"""真实启动并模拟窗口进程被结束；仅在空闲端口运行。"""
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

def test_cmd_files_use_windows_line_endings():
    for name in ["一键启动.cmd", "停止服务.cmd", "检测系统.cmd"]:
        content = (ROOT / name).read_bytes()
        assert b"\r\n" in content
        assert b"\n" not in content.replace(b"\r\n", b""), "CMD 中文脚本必须使用 CRLF"

@pytest.mark.skipif(os.name != "nt", reason="Windows 窗口生命周期验证")
def test_window_exit_stops_server():
    def online():
        try:
            urllib.request.urlopen("http://127.0.0.1:8765/api/health", timeout=.5).close()
            return True
        except OSError:
            return False
    if online():
        pytest.skip("端口已有服务，不干扰现有窗口")
    launcher = subprocess.Popen([sys.executable, "launcher.py", "--no-browser"], cwd=ROOT,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(120):
            if online():
                break
            if launcher.poll() is not None:
                pytest.fail(launcher.stdout.read().decode("utf-8", "replace"))
            time.sleep(.25)
        assert online()
        launcher.kill()  # 模拟关闭窗口，无法依赖 Python finally。
        launcher.wait(timeout=5)
        for _ in range(40):
            if not online():
                break
            time.sleep(.1)
        assert not online(), "启动窗口退出后服务不应继续运行"
    finally:
        if launcher.poll() is None:
            launcher.kill()
            launcher.wait(timeout=5)
