import importlib
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from server import integration
from cli import Client

backend = importlib.import_module('server.app')


@pytest.fixture
def remote(tmp_path, monkeypatch):
    monkeypatch.delenv('VIDEOMATTING_API_TOKEN', raising=False)
    monkeypatch.setattr(integration, 'TOKEN_PATH', tmp_path / 'api-token.txt')
    monkeypatch.setattr(integration, 'LOG_ROOT', tmp_path)
    monkeypatch.setattr(backend, 'DATA', tmp_path / 'jobs')
    backend.DATA.mkdir()
    monkeypatch.setattr(backend, 'jobs', {})
    monkeypatch.setattr(backend, 'cancel_events', {})
    monkeypatch.setattr(backend, 'executor', Mock())
    with TestClient(backend.app) as client:
        client.headers['Authorization'] = 'Bearer ' + integration.api_token()
        yield client, tmp_path


def test_token_persists_and_all_integrated_endpoints_require_it(remote):
    client, root = remote
    token = integration.api_token()
    assert token == integration.api_token() == (root / 'api-token.txt').read_text().strip()
    for path in ['/api/v1/health', '/api/v1/jobs', '/api/v1/logs/server-error', '/api/v1/diagnostics']:
        assert client.get(path, headers={'Authorization': 'Bearer invalid'}).status_code == 401
    assert client.get('/api/v1/health').json()['api_version'] == '1'
    # 原网页仍可使用现有接口。
    assert client.get('/api/health', headers={'Authorization': ''}).status_code == 200
    schema = client.get('/openapi.json').json()
    assert schema['paths']['/api/v1/jobs']['post']['security']


def test_task_controls_events_and_protected_download(remote):
    client, root = remote
    response = client.post('/api/v1/jobs', files={'file': ('远程视频.mp4', b'video')})
    assert response.status_code == 202
    job = response.json()
    base = '/api/v1/jobs/' + job['id']
    assert client.get(base).json()['name'] == '远程视频.mp4'
    assert client.post(base + '/cancel').json()['status'] == 'cancelled'
    assert client.post(base + '/resume').status_code == 202
    assert client.get(base + '/logs').json()['exists']
    assert client.get(base + '/logs?kind=error').json()['exists'] is False
    backend.save(job['id'], status='done')
    (backend.DATA / job['id'] / 'result.webm').write_bytes(b'complete-webm')
    links = client.post(base + '/export', json={'mode': 'transparent'}).json()
    assert links['download'] == base + '/download'
    assert client.get(links['download']).content == b'complete-webm'
    assert client.get(links['download'], headers={'Authorization': ''}).status_code == 401
    assert client.delete(base).json()['files_deleted']
    assert not (backend.DATA / job['id']).exists()


def test_hidden_task_access_and_bounded_allowlisted_logs(remote):
    client, root = remote
    job = client.post('/api/v1/jobs', files={'file': ('x.mp4', b'x')}).json()
    backend.save(job['id'], status='failed', history_hidden=True)
    assert client.get('/api/v1/jobs').json() == []
    assert len(client.get('/api/v1/jobs?include_hidden=true').json()) == 1
    (root / 'server-error.log').write_text('第一行\n第二行\n第三行\n', encoding='utf-8')
    result = client.get('/api/v1/logs/server-error?lines=2').json()
    assert result['text'] == '第二行\n第三行' and result['truncated']
    assert client.get('/api/v1/logs/api-token').status_code == 422
    assert client.get('/api/v1/logs/server?lines=3000').status_code == 422
    (root / 'server-error.log').write_text('文' * 300000, encoding='utf-8')
    tail = client.get('/api/v1/logs/server-error').json()['text']
    assert tail and len(tail.encode()) <= 256 * 1024


def test_failure_keeps_error_log_and_does_not_log_keys(remote, monkeypatch):
    client, root = remote
    job = client.post('/api/v1/jobs', files={'file': ('x.mp4', b'x')}).json()
    monkeypatch.setattr(backend.engine, 'process', Mock(side_effect=RuntimeError('模型测试错误')))
    backend.execute(job['id'])
    result = client.get(f'/api/v1/jobs/{job["id"]}/logs?kind=error').json()
    assert '模型测试错误' in result['text'] and 'Traceback' in result['text']
    assert integration.api_token() not in result['text']


def test_diagnostic_is_async_and_single_instance(remote, monkeypatch):
    client, root = remote
    started, release = threading.Event(), threading.Event()
    def check(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(integration.subprocess, 'run', check)
    monkeypatch.setattr(integration, '_diagnostic', {'status': 'idle'})
    try:
        assert client.post('/api/v1/diagnostics/system-check').status_code == 202
        assert started.wait(2)
        assert client.post('/api/v1/diagnostics/system-check').status_code == 409
        assert client.get('/api/v1/diagnostics/system-check').json()['status'] == 'running'
    finally:
        release.set()
    for _ in range(100):
        if integration.diagnostic_status()['status'] != 'running':
            break
        threading.Event().wait(.01)
    assert integration.diagnostic_status()['status'] == 'done'


def test_cli_calls_same_endpoints_and_download_does_not_overwrite(remote, monkeypatch, tmp_path):
    connection, root = remote
    class Response:
        def __init__(self, response):
            self.data = response.content
            self.offset = 0
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size=-1):
            end = len(self.data) if size < 0 else self.offset + size
            result = self.data[self.offset:end]
            self.offset = min(end, len(self.data))
            return result
    client = Client('http://testserver', integration.api_token())
    def request(path, method='GET', data=None):
        response = connection.request(method, path, json=data)
        response.raise_for_status()
        return Response(response)
    monkeypatch.setattr(client, 'request', request)
    job = connection.post('/api/v1/jobs', files={'file': ('x.mp4', b'x')}).json()
    backend.save(job['id'], status='done')
    (backend.DATA / job['id'] / 'result.webm').write_bytes(b'valid-result')
    assert client.wait(job['id'])['status'] == 'done'
    target = tmp_path / 'download.webm'
    client.download(job['id'], output=target)
    assert target.read_bytes() == b'valid-result'
    with pytest.raises(FileExistsError):
        client.download(job['id'], output=target)
    assert target.read_bytes() == b'valid-result'


def test_incomplete_cli_download_never_publishes_or_overwrites(monkeypatch, tmp_path):
    client = Client('http://testserver', 'test-token')
    monkeypatch.setattr(client, 'json', lambda *args, **kwargs: {'download': '/api/v1/jobs/test/download'})
    class Incomplete:
        headers = {'Content-Length': '100'}
        def __init__(self): self.sent = False
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size):
            if self.sent: return b''
            self.sent = True
            return b'partial'
    monkeypatch.setattr(client, 'request', lambda *args, **kwargs: Incomplete())
    target = tmp_path / 'result.webm'
    with pytest.raises(OSError, match='不完整'):
        client.download('test', output=target)
    assert not target.exists() and not list(tmp_path.glob('*.part'))
    target.write_bytes(b'old-valid-result')
    with pytest.raises(OSError, match='不完整'):
        client.download('test', output=target, overwrite=True)
    assert target.read_bytes() == b'old-valid-result'


def test_cli_does_not_forward_key_on_redirect():
    import http.server
    import urllib.error
    leaked = []
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.endswith('/health'):
                self.send_response(302)
                self.send_header('Location', '/leak')
                self.end_headers()
            else:
                leaked.append(self.headers.get('Authorization'))
                self.send_response(200)
                self.end_headers()
        def log_message(self, *args): pass
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = Client(f'http://127.0.0.1:{server.server_port}', 'do-not-leak')
        with pytest.raises(urllib.error.HTTPError) as caught:
            client.json('/api/v1/health')
        assert caught.value.code == 302 and leaked == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_release_versions_agree():
    from pathlib import Path
    import cli
    root = Path(__file__).resolve().parents[1]
    assert backend.VERSION == cli.VERSION == json.loads((root / 'web/package.json').read_text())['version'] == '1.1.0'
    assert f'title 清影工坊 {backend.VERSION}' in (root / '一键启动.cmd').read_text(encoding='utf-8')
    assert backend.app.openapi()['info']['version'] == backend.VERSION


def test_cli_output_is_utf8_even_when_redirected_with_legacy_encoding():
    import os
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'PYTHONIOENCODING': 'gbk'}
    result = subprocess.run([sys.executable, 'cli.py', '--help'], cwd=root, env=env, capture_output=True, check=True)
    assert '远程任务与诊断客户端' in result.stdout.decode('utf-8')
