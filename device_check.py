"""源码与绿色版共用的系统检测入口。"""
import contextlib
import datetime
import os
import platform
import shutil
import subprocess
import sys
import traceback
from pathlib import Path


ROOT = Path(__file__).resolve().parent
LOG_PATH = ROOT / 'data/system-check.log'


class Tee:
    def __init__(self, console, log):
        self.console = console
        self.log = log

    def write(self, text):
        if self.console:
            self.console.write(text)
        self.log.write(text)
        self.log.flush()
        return len(text)

    def flush(self):
        if self.console:
            self.console.flush()
        self.log.flush()


def nvidia_information():
    candidates = [shutil.which('nvidia-smi')]
    for base in (os.environ.get('SystemRoot'), os.environ.get('ProgramFiles')):
        if base:
            candidates.extend((str(Path(base) / 'System32/nvidia-smi.exe'),
                               str(Path(base) / 'NVIDIA Corporation/NVSMI/nvidia-smi.exe')))
    executable = next((path for path in candidates if path and Path(path).is_file()), None)
    if not executable:
        print('NVIDIA 驱动检测：未找到 nvidia-smi，无法确认驱动版本。')
        return
    print('NVIDIA 驱动与设备信息：')
    try:
        result = subprocess.run([executable], capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=15,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        print(result.stdout.strip() or '无输出')
        if result.stderr:
            print('驱动检测错误：', result.stderr.strip())
        print('驱动检测返回码：', result.returncode)
    except (OSError, subprocess.TimeoutExpired) as error:
        print('驱动检测失败：', error)


def diagnose():
    print('清影工坊 — 系统检测')
    print('检测时间：', datetime.datetime.now().astimezone().isoformat(timespec='seconds'))
    print('Python：', sys.version)
    print('运行路径：', sys.executable)
    print('系统：', platform.platform())
    print('处理器：', platform.processor() or '未识别')
    try:
        import psutil
        memory = psutil.virtual_memory()
        print('内存：总计', round(memory.total / 2**30, 1), 'GB；可用', round(memory.available / 2**30, 1), 'GB')
    except ImportError:
        print('内存监控依赖尚未安装。')
    nvidia_information()
    try:
        import torch
    except ImportError:
        print('处理依赖尚未安装，请运行「一键启动.cmd」准备环境。')
        return 1
    print('PyTorch：', torch.__version__)
    print('PyTorch 路径：', torch.__file__)
    print('内置 CUDA：', torch.version.cuda or '未包含（当前为 CPU 运行库）')
    print('CUDA_VISIBLE_DEVICES：', os.environ.get('CUDA_VISIBLE_DEVICES', '未设置'))
    if torch.cuda.is_available():
        try:
            print('显卡：', torch.cuda.get_device_name(0))
            print('显存：', round(torch.cuda.get_device_properties(0).total_memory / 2**30, 1), 'GB')
            with torch.inference_mode():
                value = torch.ones((256, 256), device='cuda')
                result = value @ value
                torch.cuda.synchronize()
                assert result[0, 0].item() == 256
            print('GPU 运算通过，程序将自动使用 CUDA GPU。')
        except Exception as error:
            print('GPU 运算失败，请核对 NVIDIA 驱动及显卡兼容性：', error)
            traceback.print_exc()
            return 1
    else:
        print('当前使用 CPU 处理，建议使用具备 NVIDIA 显卡的设备。')
        if torch.version.cuda:
            print('如有 RTX 4060，请核对 NVIDIA 驱动。')
            print('CUDA 初始化详细诊断（独立进程）：')
            try:
                result = subprocess.run([sys.executable, '-c',
                                         'import torch; torch.cuda.init(); print("CUDA initialization succeeded")'],
                                        capture_output=True, text=True, encoding='utf-8', errors='replace',
                                        timeout=30, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                print('CUDA 诊断退出码：', result.returncode)
                if result.stdout:
                    print(result.stdout.strip())
                if result.stderr:
                    print(result.stderr.strip())
                if result.returncode and not result.stderr:
                    print('CUDA 初始化进程异常退出，未提供 Python 错误堆栈。请结合 NVIDIA 驱动信息检查。')
            except (OSError, subprocess.TimeoutExpired) as error:
                print('CUDA 诊断失败：', error)
            if torch.version.cuda.startswith('13.'):
                print('CUDA 13.x 需要兼容的 R580 或更新 NVIDIA 驱动，请以官方兼容要求为准。')
        else:
            print('GPU 加速需要 CUDA 版 PyTorch；二合一绿色版已内置。')
    models = ('models/matanyone2.pth', 'models/sam2.1_hiera_tiny.pt',
              'models/torch/hub/checkpoints/deeplabv3_mobilenet_v3_large-fc3c493d.pth')
    missing = [name for name in models if not (ROOT / name).is_file()]
    print('模型：', '齐备' if not missing else '缺失：' + '、'.join(missing))
    return 1 if missing else 0


def main():
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 5 * 1024 * 1024:
            LOG_PATH.replace(LOG_PATH.with_suffix('.previous.log'))
        with LOG_PATH.open('a', encoding='utf-8') as log:
            log.write('\n' + '=' * 60 + '\n')
            with contextlib.redirect_stdout(Tee(sys.stdout, log)), contextlib.redirect_stderr(Tee(sys.stderr, log)):
                try:
                    result = diagnose()
                except Exception:
                    print('系统检测发生异常：')
                    traceback.print_exc()
                    result = 1
                print('检测日志已保存：', LOG_PATH)
                return result
    except OSError as error:
        print('无法保存检测日志：', error)
        return diagnose()


if __name__ == '__main__':
    raise SystemExit(main())
