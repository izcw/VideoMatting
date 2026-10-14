"""源码首次启动时准备环境；绿色版直接使用内置运行环境。"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYTHON = ROOT / '.venv/Scripts/python.exe'
REQUIRED = ('models/matanyone2.pth', 'models/sam2.1_hiera_tiny.pt',
            'models/torch/hub/checkpoints/deeplabv3_mobilenet_v3_large-fc3c493d.pth',
            'vendor/MatAnyone2/matanyone2/__init__.py', 'vendor/SAM2/sam2/__init__.py',
            'web/dist/index.html')


def ready():
    if not PYTHON.exists() or not all((ROOT / name).is_file() for name in REQUIRED):
        return False
    return subprocess.run([str(PYTHON), '-c', 'import torch, torchvision, fastapi, uvicorn, cv2, imageio_ffmpeg'],
                          cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    if args.check:
        return 0 if ready() else 1
    if sys.version_info[:2] != (3, 13):
        print('源码首次安装需要 Python 3.13，请安装后再次运行一键启动。')
        return 1
    pnpm = shutil.which('pnpm.cmd')
    if not shutil.which('node') or not pnpm:
        print('源码首次安装需要 Node.js 和 pnpm，请安装后再次运行一键启动。')
        return 1
    try:
        if not PYTHON.exists():
            subprocess.run([sys.executable, '-m', 'venv', str(ROOT / '.venv')], check=True)
        print('正在安装处理依赖...', flush=True)
        subprocess.run([str(PYTHON), '-m', 'pip', 'install', '-r', 'requirements.lock.txt'], cwd=ROOT, check=True)
        print('正在准备并校验模型...', flush=True)
        subprocess.run([str(PYTHON), 'bootstrap.py'], cwd=ROOT, check=True)
        print('正在准备网页...', flush=True)
        subprocess.run(['cmd.exe', '/c', pnpm, '--dir', 'web', 'install', '--frozen-lockfile'], cwd=ROOT, check=True)
        subprocess.run(['cmd.exe', '/c', pnpm, '--dir', 'web', 'build'], cwd=ROOT, check=True)
        print('运行环境准备完成，正在启动。', flush=True)
        return 0
    except (OSError, subprocess.CalledProcessError) as error:
        print('运行环境准备失败：', error)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
