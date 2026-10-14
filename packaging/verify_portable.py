"""在随机路径解压并验证绿色包；使用测试端口，不触碰已有服务。"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

archive = Path(sys.argv[1]).resolve()
with tempfile.TemporaryDirectory(prefix='清影 绿色版验收 ') as temporary:
    root = Path(temporary)
    with zipfile.ZipFile(archive) as package:
        package.extractall(root)
    folder = next(root.iterdir())
    python = folder / 'runtime/python.exe'
    env = {k: v for k, v in os.environ.items() if not k.startswith(('PYTHON', 'CONDA', 'VIRTUAL_ENV', 'HF_', 'TORCH_'))}
    env.update(PATH=str(Path(os.environ['SystemRoot']) / 'System32'), VIDEOMATTING_PORT='18765', PYTHONUTF8='1')
    code = '''
import sys, json, threading
from pathlib import Path
import torch, cv2, PIL, scipy, uvicorn, fastapi
from server.pipeline import ROOT, MattingEngine, run_media, inspect_video
build = json.loads((ROOT / 'build-info.json').read_text()) if (ROOT / 'build-info.json').exists() else {}
if build.get('cuda'):
    assert torch.version.cuda == build['cuda']
    assert '+cu130' in torch.__version__
assert Path(sys.prefix).resolve() == ROOT / 'runtime', sys.prefix
assert all(not p or Path(p).resolve().is_relative_to(ROOT) for p in sys.path), sys.path
engine = MattingEngine()
device = engine.load(lambda **kw: None)
from matanyone2.inference.inference_core import InferenceCore
with torch.inference_mode():
    p = InferenceCore(engine.model, cfg=engine.config)
    image = torch.rand(3, 128, 128, device=device)
    mask = torch.zeros(128, 128, device=device); mask[20:100, 20:100] = 255
    p.step(image, mask, objects=[1])
    result = p.step(image, first_frame_pred=True)
    assert torch.isfinite(result).all()
folder = ROOT / 'data'; folder.mkdir(exist_ok=True)
video = folder / 'portable-test.webm'
run_media(['-f','lavfi','-i','color=red:s=64x64:r=2:d=1','-vf','format=yuva420p','-c:v','libvpx-vp9','-auto-alt-ref','0',str(video)], threading.Event())
assert inspect_video(video)['frames'] == 2
print(json.dumps({'imports':True,'isolated':True,'models':True,'inference':True,'ffmpeg':True,'device':device,'torch':torch.__version__,'cuda_build':torch.version.cuda}))
'''
    check = subprocess.run([str(python), '-c', code], cwd=folder, env=env, capture_output=True, timeout=180)
    print(check.stdout.decode('utf-8', 'replace'), flush=True)
    if check.returncode:
        raise RuntimeError(check.stderr.decode('utf-8', 'replace'))
    # 调用实际 CMD 入口，而不是仅调用 Python 启动函数。
    log = root / 'startup.log'
    with log.open('wb') as output:
        process = subprocess.Popen(['cmd.exe', '/c', str(folder / '启动清影工坊.cmd'), '--no-browser'], cwd=folder, env=env,
                                   stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        url = 'http://127.0.0.1:18765'
        try:
            for _ in range(60):
                try:
                    with urllib.request.urlopen(url + '/api/health', timeout=1) as response:
                        health = json.load(response)
                    assert health['ready'] and health['version'] == '1.0.0'
                    break
                except OSError:
                    if process.poll() is not None:
                        raise RuntimeError(log.read_text(encoding='utf-8', errors='replace'))
                    time.sleep(1)
            else:
                raise RuntimeError('绿色版启动超时')
            assert urllib.request.urlopen(url).status == 200
            # 结束实际持有 Job Object 的 Python 启动器，模拟关闭启动窗口。
            import psutil
            children = psutil.Process(process.pid).children(recursive=True)
            launcher = next(p for p in children if 'launcher.py' in p.cmdline())
            launcher.kill()
            process.wait(timeout=15)
            for _ in range(30):
                try:
                    urllib.request.urlopen(url + '/api/health', timeout=.5).close()
                    time.sleep(.2)
                except OSError:
                    break
            else:
                raise RuntimeError('关闭启动器后服务未退出')
            print('PASS: CMD startup, HTTP page, health, window-exit shutdown', flush=True)
        finally:
            if process.poll() is None:
                subprocess.run([str(python), 'launcher.py', '--stop'], cwd=folder, env=env, timeout=20)
                process.terminate()
                process.wait(timeout=10)
