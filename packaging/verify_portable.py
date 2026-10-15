"""在随机路径解压并验证完整打包版；使用测试端口，不触碰已有服务。"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
import uuid
from pathlib import Path

archive = Path(sys.argv[1]).resolve()
with tempfile.TemporaryDirectory(prefix='清影 完整打包版验收 ') as temporary:
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
    # 准备下载测试夹具；单独的真实模型推理已在上面的检查中完成。
    fixture_id = str(uuid.uuid4())
    fixture = folder / 'data/jobs' / fixture_id
    fixture.mkdir(parents=True)
    sample = (folder / 'data/portable-test.webm').read_bytes()
    (fixture / 'result.webm').write_bytes(sample)
    (fixture / 'job.json').write_text(json.dumps(dict(id=fixture_id, name='下载测试.webm',
        status='done', created_at=time.time(), progress=100, elapsed_seconds=0)), encoding='utf-8')
    log = root / 'startup.log'
    with log.open('wb') as output:
        process = subprocess.Popen(['cmd.exe', '/c', str(folder / '一键启动.cmd'), '--no-browser'], cwd=folder, env=env,
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
                raise RuntimeError('完整打包版启动超时')
            assert urllib.request.urlopen(url).status == 200
            addresses = health.get('lan_addresses', [])
            if addresses:
                lan_url = f'http://{addresses[0]}:18765'
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                assert opener.open(lan_url, timeout=5).status == 200
                # 真实局域网 HTTP 上传有效视频；任务只用于该临时目录验收。
                boundary = 'VideoMattingPortableVerification'
                body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="lan.webm"\r\n'
                        'Content-Type: video/webm\r\n\r\n').encode() + (folder / 'data/portable-test.webm').read_bytes() + f'\r\n--{boundary}--\r\n'.encode()
                request = urllib.request.Request(lan_url + '/api/jobs', data=body, headers={
                    'Origin': lan_url, 'Content-Type': f'multipart/form-data; boundary={boundary}'})
                with opener.open(request, timeout=10) as response:
                    assert response.status == 202
                    job = json.load(response)
                opener.open(urllib.request.Request(lan_url + f'/api/jobs/{job["id"]}/cancel', data=b'',
                                                  headers={'Origin': lan_url}), timeout=10).close()
                print('PASS: LAN page and real video upload', flush=True)
            api_url = f'http://{addresses[0]}:18765' if addresses else url
            token_file = root / 'client-token.txt'
            token_file.write_text(env.get('VIDEOMATTING_API_TOKEN') or (folder / 'data/api-token.txt').read_text(), encoding='utf-8')
            def remote_cli(*arguments):
                result = subprocess.run([str(python), 'cli.py', '--server', api_url, '--token-file', str(token_file),
                                         *arguments], cwd=folder, env=env, capture_output=True, timeout=30)
                if result.returncode:
                    raise RuntimeError(result.stderr.decode('utf-8', 'replace'))
                return json.loads(result.stdout)
            assert remote_cli('health')['api_version'] == '1'
            uploaded = remote_cli('upload', str(folder / 'data/portable-test.webm'))
            assert remote_cli('status', uploaded['id'])['id'] == uploaded['id']
            assert remote_cli('cancel', uploaded['id'])['id'] == uploaded['id']
            assert remote_cli('logs', '--job', uploaded['id'])['exists']
            assert remote_cli('logs', '--name', 'server-error')['exists']
            downloaded = root / 'cli-download.webm'
            remote_cli('download', fixture_id, '--output', str(downloaded))
            assert downloaded.read_bytes() == sample
            print('PASS: authenticated CLI LAN upload, status, cancel, logs and byte-identical download', flush=True)
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
