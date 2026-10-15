"""从已验证环境制作自包含目录；不收集用户数据、系统凭证或开发机全局包。"""
import hashlib
import argparse
import json
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / 'releases' / 'VideoMatting-1.0.0-win64'


def copy_tree(source, target):
    shutil.copytree(source, target, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.git', '.pytest_cache'))


def write_entrypoints(cuda):
    for name in ('一键启动.cmd', '停止服务.cmd', '检测系统.cmd', 'device_check.py', 'launcher.py', 'cli.py', 'API接口说明.md'):
        shutil.copy2(ROOT / name, DEST / name)
    text = (ROOT / 'packaging/使用说明.txt').read_text(encoding='utf-8')
    if cuda:
        text = text.replace('Windows x64 CPU 完整打包版', 'Windows x64 GPU + CPU 完整打包版')
        text = text.replace('此包内置 CPU 运行库，即使有 NVIDIA 显卡也不会自动启用 CUDA。', '此包内置 PyTorch 2.14.1 + CUDA 13.0，自动检测 NVIDIA CUDA GPU；不可用时使用 CPU。\n使用 GPU 需安装兼容 CUDA 13.0 的 NVIDIA 驱动；无需另装 CUDA Toolkit。\nAMD/Intel 显卡不提供 GPU 加速，与当前源程序支持范围一致。\n显存不足仍可能暂停或报错，不会为了运行而自动降低分辨率或画质。\n当前打包机无 NVIDIA GPU，GPU 真机运行请在目标电脑验收。')
    (DEST / '使用说明.txt').write_bytes(text.replace('\r\n', '\n').replace('\n', '\r\n').encode('utf-8'))


def archive_package():
    def included(path):
        parts = path.relative_to(DEST).parts
        return path.is_file() and parts[0] != 'data' and '__pycache__' not in parts and path.suffix != '.pyc'

    manifest = {}
    for path in sorted(DEST.rglob('*')):
        if included(path) and path.name != '文件校验.json':
            with path.open('rb') as stream:
                manifest[path.relative_to(DEST).as_posix()] = hashlib.file_digest(stream, 'sha256').hexdigest()
    (DEST / '文件校验.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding='utf-8')
    archive = DEST.with_name(DEST.name + '.zip')
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=5) as package:
        for path in sorted(DEST.rglob('*')):
            if included(path):
                package.write(path, Path(DEST.name) / path.relative_to(DEST))
    with archive.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    archive.with_suffix('.zip.sha256').write_text(f'{digest}  {archive.name}\n', encoding='utf-8')
    print(json.dumps(dict(folder=str(DEST), archive=str(archive), bytes=archive.stat().st_size, sha256=digest)), flush=True)


def main():
    global DEST
    parser = argparse.ArgumentParser()
    parser.add_argument('--cuda', action='store_true')
    parser.add_argument('--refresh-entrypoints', action='store_true', help='同步已有发布目录的三个入口并重新压缩')
    args = parser.parse_args()
    if not args.cuda:
        DEST = ROOT / 'releases' / 'VideoMatting-1.0.0-win64-cpu'
    if args.refresh_entrypoints:
        build = json.loads((DEST / 'build-info.json').read_text(encoding='utf-8'))
        if bool(build.get('cuda')) != args.cuda:
            raise SystemExit('已有运行库与所选打包类型不一致。')
        write_entrypoints(args.cuda)
        archive_package()
        return
    if DEST.exists():
        raise SystemExit('目标已存在，请使用新的发布目录，避免覆盖。')
    DEST.mkdir(parents=True)
    base = Path(sys.base_prefix)
    runtime = DEST / 'runtime'
    runtime.mkdir()
    for path in base.iterdir():
        if path.suffix.lower() in {'.exe', '.dll'} or path.name == 'LICENSE.txt':
            shutil.copy2(path, runtime / path.name)
    copy_tree(base / 'DLLs', runtime / 'DLLs')
    shutil.copytree(base / 'Lib', runtime / 'Lib', ignore=shutil.ignore_patterns('site-packages', '__pycache__', '*.pyc', 'test', 'tests', 'idlelib', 'tkinter', 'ensurepip'))
    packages = Path(sys.prefix) / 'Lib/site-packages'
    # PyTorch 的第三方许可目录嵌套极深；平铺保留许可，避免 Windows 长路径解压失败。
    ignored = ['__pycache__', '*.pyc', 'licenses']
    if args.cuda:
        ignored += ['torch', 'torchvision', 'torch-*.dist-info', 'torchvision-*.dist-info']
    shutil.copytree(packages, runtime / 'Lib/site-packages', ignore=shutil.ignore_patterns(*ignored))
    notices = runtime / 'third-party-licenses'
    notices.mkdir()
    license_index = {}
    for directory in packages.glob('*.dist-info/licenses'):
        if args.cuda and directory.parent.name.startswith(('torch-', 'torchvision-')):
            continue
        for path in directory.rglob('*'):
            if path.is_file():
                origin = path.relative_to(packages).as_posix()
                name = hashlib.sha256(origin.encode()).hexdigest()[:20] + '.txt'
                shutil.copy2(path, notices / name)
                license_index[name] = origin
    if args.cuda:
        for pattern in ('torch-2.14.1+cu130-*-win_amd64.whl', 'torchvision-0.29.1+cu130-*-win_amd64.whl'):
            wheel, = (ROOT / 'releases/cuda-wheels').glob(pattern)
            with zipfile.ZipFile(wheel) as package:
                for member in package.infolist():
                    if member.is_dir():
                        continue
                    if '/licenses/' in member.filename:
                        name = hashlib.sha256(member.filename.encode()).hexdigest()[:20] + '.txt'
                        target = notices / name
                        license_index[name] = member.filename
                    else:
                        target = runtime / 'Lib/site-packages' / member.filename
                        if not target.resolve().is_relative_to(runtime.resolve()):
                            raise ValueError('安装包路径无效')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with package.open(member) as source, target.open('wb') as output:
                        shutil.copyfileobj(source, output)
    (notices / 'index.json').write_text(json.dumps(license_index, indent=2), encoding='utf-8')
    (runtime / 'python313._pth').write_text('.\nLib\nDLLs\nLib/site-packages\n..\nimport site\n', encoding='utf-8')
    for name in ('server', 'vendor'):
        copy_tree(ROOT / name, DEST / name)
    copy_tree(ROOT / 'web/dist', DEST / 'web/dist')
    for name in ('launcher.py', 'models-manifest.json', 'requirements.lock.txt'):
        shutil.copy2(ROOT / name, DEST / name)
    for name in ('matanyone2.pth', 'sam2.1_hiera_tiny.pt', 'torch/hub/checkpoints/deeplabv3_mobilenet_v3_large-fc3c493d.pth'):
        target = DEST / 'models' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / 'models' / name, target)
    write_entrypoints(args.cuda)
    (DEST / 'build-info.json').write_text(json.dumps(dict(version='1.0.0', torch='2.14.1', torchvision='0.29.1', cuda='13.0' if args.cuda else None, models_bundled=True), indent=2), encoding='utf-8')
    archive_package()


if __name__ == '__main__':
    main()
