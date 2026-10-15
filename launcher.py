"""启动窗口持有 Windows 作业对象；窗口退出自动结束服务及子进程。"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path
from server.network import lan_addresses

ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("VIDEOMATTING_PORT", "8765"))
URL = f"http://127.0.0.1:{PORT}"
PID_FILE = ROOT / "data" / "server.pid"


class ServerLifetime:
    def __init__(self):
        self.handle = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes
        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("minimum", ctypes.c_size_t),
                        ("maximum", ctypes.c_size_t), ("active", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]
        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", ctypes.c_uint64 * 6),
                        ("memory", ctypes.c_size_t * 4)]
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.handle or not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, process):
        if self.handle and not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            import ctypes
            process.terminate()
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def health():
    with urllib.request.urlopen(URL + "/api/health", timeout=2) as response:
        info = json.load(response)
    if info.get("service") != "py-VideoMatting":
        raise RuntimeError(f"{PORT} 端口已被其他程序占用。")
    return info


def stop():
    import psutil
    if not PID_FILE.exists():
        print("没有正在运行的本程序服务。")
        return
    try:
        process = psutil.Process(int(PID_FILE.read_text().strip()))
        if "server.app:app" not in process.cmdline() or Path(process.cwd()).resolve() != ROOT:
            raise RuntimeError("进程不属于本程序，已拒绝关闭。")
        for child in process.children(recursive=True):
            child.terminate()
        process.terminate()
        process.wait(timeout=10)
        PID_FILE.unlink(missing_ok=True)
        print("视频抠像服务已关闭。")
    except psutil.NoSuchProcess:
        PID_FILE.unlink(missing_ok=True)
        print("服务已经关闭。")


def start(no_browser=False, lifetime=None):
    server = None
    if not (ROOT / "web/dist/index.html").is_file():
        raise RuntimeError("网页文件缺失。源码请重新运行「一键启动.cmd」；完整打包版请完整解压。")
    try:
        info = health()
        print("服务已在其他启动窗口运行；请关闭原窗口来停止服务。")
    except (OSError, TimeoutError):
        (ROOT / "data").mkdir(exist_ok=True)
        with (ROOT / "data/server.log").open("ab") as output, (ROOT / "data/server-error.log").open("ab") as errors:
            flags = 0
            if os.name == "nt":
                flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            server = subprocess.Popen([sys.executable, "-m", "uvicorn", "server.app:app",
                                       "--host", "0.0.0.0", "--port", str(PORT)],
                                      cwd=ROOT, stdin=subprocess.DEVNULL, stdout=output, stderr=errors,
                                      creationflags=flags, close_fds=True)
            lifetime.assign(server)
        PID_FILE.write_text(str(server.pid))
        for _ in range(60):
            if server.poll() is not None:
                raise RuntimeError("服务启动失败，请查看 data/server-error.log。")
            time.sleep(1)
            try:
                info = health()
                break
            except (OSError, TimeoutError):
                continue
        else:
            raise RuntimeError("服务启动超时，请查看 data/server-error.log。")
    addresses = lan_addresses()
    access_url = f'http://{addresses[0]}:{PORT}' if addresses else URL
    print("视频抠像工作台已启动：" + access_url, flush=True)
    for address in addresses[1:]:
        print(f"其他局域网地址：http://{address}:{PORT}", flush=True)
    print("本机访问：" + URL, flush=True)
    if addresses:
        print("同一局域网设备可访问以上地址；视频由启动服务的这台电脑处理，任务与历史共用。", flush=True)
        print(f"其他设备无法访问时，请允许 Windows 防火墙的专用网络访问 TCP {PORT} 端口。", flush=True)
    else:
        print("未发现局域网 IPv4 地址，请检查网络连接后重新启动。", flush=True)
    print("处理设备：" + info["device"], flush=True)
    if not info["ready"]:
        print("模型文件缺失。源码请重新运行「一键启动.cmd」；完整打包版请完整解压。")
    if not no_browser:
        if not webbrowser.open(access_url):
            print("无法自动打开浏览器，请手动打开上方网址。")
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    try:
        import portalocker
        (ROOT / "data").mkdir(exist_ok=True)
        lifetime = ServerLifetime()
        try:
            with portalocker.Lock(ROOT / "data/launcher.lock", timeout=70):
                server = None if args.stop else start(args.no_browser, lifetime)
                if args.stop:
                    stop()
            if server:
                print("请保持此窗口打开。关闭窗口或按 Ctrl+C，会停止服务及正在处理的任务。", flush=True)
                try:
                    while server.poll() is None:
                        time.sleep(.5)
                except KeyboardInterrupt:
                    print("正在关闭服务…", flush=True)
                finally:
                    lifetime.close()
                    if os.name != "nt" and server.poll() is None:
                        server.terminate()
                    server.wait(timeout=10)
                    if PID_FILE.exists() and PID_FILE.read_text().strip() == str(server.pid):
                        PID_FILE.unlink(missing_ok=True)
                    print("服务已关闭。", flush=True)
        finally:
            lifetime.close()
    except Exception as error:
        print("错误：" + str(error))
        sys.exit(1)
