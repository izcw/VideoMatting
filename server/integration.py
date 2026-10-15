"""带密钥的远程集成接口，复用网页的任务处理实现。"""
import hmac
import os
import platform
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

ROOT = Path(__file__).resolve().parents[1]
TOKEN_PATH = ROOT / 'data/api-token.txt'
LOG_ROOT = ROOT / 'data'
bearer = HTTPBearer(auto_error=False)
_token_lock = threading.Lock()
_diagnostic_lock = threading.Lock()
_diagnostic = {'status': 'idle'}


def api_token():
    configured = os.environ.get('VIDEOMATTING_API_TOKEN', '').strip()
    if configured:
        return configured
    with _token_lock:
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            token = TOKEN_PATH.read_text(encoding='utf-8').strip()
        else:
            token = secrets.token_urlsafe(32)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                stream.write(token + '\n')
        if not token:
            raise RuntimeError('API 密钥文件为空，请停止服务后删除该文件并重新启动。')
        return token


def require_token(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    if credentials is None or not hmac.compare_digest(credentials.credentials.encode('utf-8'), api_token().encode('utf-8')):
        raise HTTPException(401, '需要有效的 API 访问密钥。', headers={'WWW-Authenticate': 'Bearer'})


def tail_log(path, lines):
    if not path.is_file():
        return {'exists': False, 'text': '', 'truncated': False}
    with path.open('rb') as stream:
        size = stream.seek(0, 2)
        offset = max(0, size - 256 * 1024)
        stream.seek(offset)
        text = stream.read().decode('utf-8', errors='replace')
    entries = text.splitlines()
    if offset and entries:
        entries = entries[1:]
    return {'exists': True, 'text': '\n'.join(entries[-lines:]),
            'truncated': offset > 0 or len(entries) > lines}


def diagnostic_status():
    with _diagnostic_lock:
        return dict(_diagnostic)


def start_diagnostic():
    with _diagnostic_lock:
        if _diagnostic['status'] == 'running':
            raise HTTPException(409, '系统检测正在运行，请查询状态。')
        _diagnostic.clear()
        _diagnostic.update(status='running', started_at=time.time())

    def run():
        try:
            result = subprocess.run([sys.executable, str(ROOT / 'device_check.py')], cwd=ROOT,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=120, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            outcome = dict(status='done' if result.returncode == 0 else 'failed', returncode=result.returncode)
        except (OSError, subprocess.TimeoutExpired) as error:
            outcome = dict(status='failed', error=str(error))
        with _diagnostic_lock:
            _diagnostic.update(**outcome, finished_at=time.time())
    threading.Thread(target=run, name='api-system-check', daemon=True).start()
    return diagnostic_status()


def install_routes(app, backend):
    router = APIRouter(prefix='/api/v1', tags=['集成 API'], dependencies=[Depends(require_token)])

    @router.get('/health')
    def health():
        return {**backend.health(), 'api_version': '1'}

    @router.get('/system')
    def system():
        return backend.system_status()

    @router.get('/diagnostics')
    def diagnostics():
        from .resources import RESOURCE_LIMIT
        return {'health': backend.health(), 'resources': backend.system_status(),
                'platform': platform.platform(), 'python': sys.version,
                'limits': dict(cpu=RESOURCE_LIMIT, gpu=RESOURCE_LIMIT, memory=RESOURCE_LIMIT, vram=RESOURCE_LIMIT),
                'jobs': backend.list_jobs(), 'system_check': diagnostic_status()}

    @router.get('/logs/{name}')
    def logs(name: Literal['server', 'server-error', 'system-check'], lines: int = Query(200, ge=1, le=2000)):
        return {'name': name, **tail_log(LOG_ROOT / (name + '.log'), lines)}

    @router.get('/diagnostics/system-check')
    def check_status():
        return diagnostic_status()

    @router.post('/diagnostics/system-check', status_code=202)
    def check_system():
        return start_diagnostic()

    @router.get('/jobs')
    def list_jobs(include_hidden: bool = False):
        with backend.lock:
            return sorted((backend.public_job(job) for job in backend.jobs.values()
                           if include_hidden or not job.get('history_hidden')),
                          key=lambda job: job['created_at'], reverse=True)

    @router.post('/jobs', status_code=202)
    async def upload(file: UploadFile = File(...)):
        return await backend.upload(file)

    @router.get('/jobs/{job_id}')
    def job(job_id: str):
        return backend.lookup(job_id)

    @router.post('/jobs/{job_id}/cancel')
    def cancel(job_id: str):
        return backend.cancel_job(job_id)

    @router.post('/jobs/{job_id}/resume', status_code=202)
    def resume(job_id: str):
        return backend.resume_job(job_id)

    @router.delete('/jobs/{job_id}')
    def delete(job_id: str):
        return backend.delete_job(job_id)

    @router.get('/jobs/{job_id}/logs')
    def job_logs(job_id: str, kind: Literal['events', 'error'] = 'events', lines: int = Query(200, ge=1, le=2000)):
        backend.lookup(job_id)
        name = 'events.jsonl' if kind == 'events' else 'error.log'
        return {'job_id': job_id, 'kind': kind, **tail_log(backend.DATA / job_id / name, lines)}

    @router.get('/jobs/{job_id}/preview-frame')
    def preview(job_id: str):
        return backend.preview_frame(job_id)

    @router.post('/jobs/{job_id}/export')
    def export(job_id: str, options: backend.ExportOptions):
        result = backend.export_job(job_id, options)
        return {key: value.replace('/api/jobs/', '/api/v1/jobs/', 1) for key, value in result.items()}

    @router.get('/jobs/{job_id}/exports/{name}')
    def exported(job_id: str, name: str, download: bool = False):
        return backend.exported_media(job_id, name, download)

    @router.get('/jobs/{job_id}/audio/{name}')
    def audio(job_id: str, name: str):
        return backend.audio_download(job_id, name)

    @router.get('/jobs/{job_id}/{asset}')
    def media(job_id: str, asset: Literal['original', 'result', 'download']):
        return backend.media(job_id, asset)

    app.include_router(router)
