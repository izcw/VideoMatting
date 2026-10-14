import importlib
import threading
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

module = importlib.import_module("server.app")


def test_resource_statistics_are_per_task_persisted_and_processing_only(client):
    import json
    first = client.post('/api/jobs', files={'file': ('first.mp4', b'video')}).json()['id']
    second = client.post('/api/jobs', files={'file': ('second.mp4', b'video')}).json()['id']
    module.record_resources(first, {'cpu': 99, 'memory': 99})
    assert 'resource_stats' not in module.jobs[first]  # 排队期间不采样。
    module.save(first, status='processing')
    module.record_resources(first, {'cpu': 20, 'memory': 40, 'gpu': None})
    module.record_resources(first, {'cpu': 60, 'memory': 50, 'gpu': {'usage': 30}})
    stats = client.get(f'/api/jobs/{first}').json()['resource_stats']
    assert stats['cpu']['max'] == 60 and stats['cpu']['average'] == 40
    assert stats['memory']['average'] == 45 and stats['gpu']['count'] == 1
    assert 'resource_stats' not in module.jobs[second]
    module.save(first, status='done')
    module.record_resources(first, {'cpu': 100, 'memory': 100})
    assert module.jobs[first]['resource_stats'] == stats
    assert json.loads((module.DATA / first / 'job.json').read_text(encoding='utf-8'))['resource_stats'] == stats
    module.save(first, status='processing')  # 同一任务重试继续累计。
    module.record_resources(first, {'cpu': 40, 'memory': 45})
    assert module.jobs[first]['resource_stats']['cpu']['count'] == 3
    assert module.jobs[first]['resource_stats']['cpu']['average'] == 40


def test_single_delete_preserves_media_and_blocks_active(client):
    job = client.post('/api/jobs', files={'file': ('test.mp4', b'original')}).json()
    path = f"/api/jobs/{job['id']}"
    assert client.delete(path).status_code == 409
    module.save(job['id'], status='failed')
    assert client.delete(path).json()['files_deleted'] is False
    assert client.get('/api/jobs').json() == []
    assert (module.DATA / job['id'] / 'source').read_bytes() == b'original'


def test_progress_preview_is_available_before_completion(client):
    job = client.post('/api/jobs', files={'file': ('test.mp4', b'original')}).json()
    from PIL import Image
    Image.new('RGBA', (8, 8), (255, 0, 0, 127)).save(module.DATA / job['id'] / 'preview.png')
    response = client.get(f"/api/jobs/{job['id']}/preview-frame")
    assert response.status_code == 200 and response.headers['content-type'] == 'image/png'
    assert response.headers['cache-control'] == 'no-store'


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "DATA", tmp_path)
    monkeypatch.setattr(module, "jobs", {})
    monkeypatch.setattr(module, "cancel_events", {})
    monkeypatch.setattr(module, "executor", Mock())
    with TestClient(module.app) as connection:
        yield connection


def test_upload_enqueues_without_using_client_filename_as_path(client):
    response = client.post("/api/jobs", files={"file": ("../outside.mp4", b"video", "video/mp4")})
    assert response.status_code == 202
    job = response.json()
    assert job["name"] == "outside.mp4"
    assert (module.DATA / job["id"] / "source").read_bytes() == b"video"
    module.executor.submit.assert_called_once()


@pytest.mark.parametrize("name, content, code", [("x.exe", b"x", 400), ("x.mp4", b"", 400)])
def test_invalid_upload(client, name, content, code):
    assert client.post("/api/jobs", files={"file": (name, content)}).status_code == code


def test_oversize_upload_removed(client, monkeypatch):
    monkeypatch.setattr(module, "MAX_UPLOAD_BYTES", 4)
    assert client.post("/api/jobs", files={"file": ("x.mp4", b"12345")}).status_code == 413
    assert not list(module.DATA.glob("*/source"))


def test_queue_limit(client):
    for _ in range(3):
        assert client.post("/api/jobs", files={"file": ("x.mp4", b"x")}).status_code == 202
    assert client.post("/api/jobs", files={"file": ("x.mp4", b"x")}).status_code == 429


def test_cancel_queued(client):
    job = client.post("/api/jobs", files={"file": ("x.mp4", b"x")}).json()
    assert client.post(f"/api/jobs/{job['id']}/cancel").json()["status"] == "cancelled"
    assert module.cancel_events[job["id"]].is_set()


def test_cross_site_mutations_rejected(client):
    assert client.post("/api/jobs", headers={"origin": "https://attacker.example"},
                       files={"file": ("x.mp4", b"x")}).status_code == 403
    assert client.post("/api/jobs", headers={"origin": "http://testserver"},
                       files={"file": ("x.mp4", b"x")}).status_code == 202


def test_path_and_arbitrary_assets_rejected(client):
    assert client.get("/api/jobs/not-a-uuid").status_code == 404
    job = client.post("/api/jobs", files={"file": ("x.mp4", b"x")}).json()
    assert client.get(f"/api/jobs/{job['id']}/source").status_code == 404
    assert client.get(f"/api/jobs/{job['id']}/download").status_code == 404


def test_ready_download_and_http_range(client):
    job = client.post("/api/jobs", files={"file": ("x.mp4", b"x")}).json()
    (module.DATA / job["id"] / "result.webm").write_bytes(b"0123456789")
    module.save(job["id"], status="done")
    response = client.get(f"/api/jobs/{job['id']}/download")
    assert response.content == b"0123456789"
    assert "attachment" in response.headers["content-disposition"]
    assert f"VideoMatting-WebM-{job['id'].replace('-', '')[:12]}.webm" in response.headers["content-disposition"]
    partial = client.get(f"/api/jobs/{job['id']}/result", headers={"range": "bytes=2-4"})
    assert partial.status_code == 206 and partial.content == b"234"


def test_failure_surfaces_error(client, monkeypatch):
    job = client.post("/api/jobs", files={"file": ("x.mp4", b"x")}).json()
    def failure(*_):
        raise ValueError("未检测到人物")
    monkeypatch.setattr(module.engine, "process", failure)
    module.execute(job["id"])
    assert client.get(f"/api/jobs/{job['id']}").json()["error"] == "未检测到人物"


def test_server_responds_with_local_page(client):
    assert client.get("/").status_code == 200
    assert client.get("/api/health").json()["service"] == "py-VideoMatting"


def test_clear_history_preserves_files_and_job_access(client):
    import json
    job = client.post('/api/jobs', files={'file': ('keep.mp4', b'original')}).json()
    folder = module.DATA / job['id']
    (folder / 'result.webm').write_bytes(b'result')
    module.save(job['id'], status='done')
    response = client.post('/api/history/clear')
    assert response.status_code == 200 and response.json() == {'cleared': 1, 'files_deleted': False}
    assert client.get('/api/jobs').json() == []
    assert client.get(f"/api/jobs/{job['id']}").status_code == 200
    assert client.get(f"/api/jobs/{job['id']}/download").content == b'result'
    assert (folder / 'source').read_bytes() == b'original'
    assert json.loads((folder / 'job.json').read_text(encoding='utf-8'))['history_hidden']
    assert client.post('/api/history/clear').json()['cleared'] == 0
    new = client.post('/api/jobs', files={'file': ('new.mp4', b'new')}).json()
    assert [j['id'] for j in client.get('/api/jobs').json()] == [new['id']]


def test_clear_history_refuses_active_tasks_and_cross_site(client):
    job = client.post('/api/jobs', files={'file': ('keep.mp4', b'original')}).json()
    assert client.post('/api/history/clear').status_code == 409
    assert len(client.get('/api/jobs').json()) == 1
    module.save(job['id'], status='done')
    assert client.post('/api/history/clear', headers={'origin': 'https://other.example'}).status_code == 403
    assert len(client.get('/api/jobs').json()) == 1


def test_history_keeps_latest_ten_without_deleting_material(client):
    ids = []
    for index in range(12):
        job = client.post('/api/jobs', files={'file': (f'{index}.mp4', b'original')}).json()
        ids.append(job['id'])
        module.save(job['id'], status='done')
    visible = client.get('/api/jobs').json()
    assert len(visible) == 10
    assert {j['id'] for j in visible} == set(ids[-10:])
    assert module.jobs[ids[0]]['history_hidden']
    assert (module.DATA / ids[0] / 'source').read_bytes() == b'original'


def test_resume_reuses_source_and_does_not_duplicate_active_job(client):
    job = client.post('/api/jobs', files={'file': ('resume.mp4', b'original')}).json()
    module.save(job['id'], status='interrupted', error='服务中断', progress=50)
    module.executor.submit.reset_mock()
    response = client.post(f"/api/jobs/{job['id']}/resume")
    assert response.status_code == 202 and response.json()['status'] == 'queued'
    assert response.json()['error'] is None and response.json()['progress'] == 50
    assert not module.cancel_events[job['id']].is_set()
    assert (module.DATA / job['id'] / 'source').read_bytes() == b'original'
    assert client.post(f"/api/jobs/{job['id']}/resume").status_code == 202
    module.executor.submit.assert_called_once()


def test_resume_missing_source_or_done_is_rejected(client):
    job = client.post('/api/jobs', files={'file': ('resume.mp4', b'original')}).json()
    module.save(job['id'], status='done')
    assert client.post(f"/api/jobs/{job['id']}/resume").status_code == 409
    module.save(job['id'], status='interrupted')
    (module.DATA / job['id'] / 'source').unlink()
    assert client.post(f"/api/jobs/{job['id']}/resume").status_code == 409


def test_restart_preserves_interrupted_tasks_for_manual_resume(tmp_path, monkeypatch):
    import json
    import uuid
    import time
    monkeypatch.setattr(module, 'DATA', tmp_path)
    monkeypatch.setattr(module, 'jobs', {})
    monkeypatch.setattr(module, 'cancel_events', {})
    mock_executor = Mock()
    monkeypatch.setattr(module, 'executor', mock_executor)
    job_id = str(uuid.uuid4())
    folder = tmp_path / job_id
    folder.mkdir()
    (folder / 'source').write_bytes(b'complete original')
    (folder / 'job.json').write_text(json.dumps({'id': job_id, 'name': 'retry.mp4', 'status': 'processing', 'created_at': time.time(), 'original_ready': False}), encoding='utf-8')
    with TestClient(module.app) as connection:
        record = connection.get(f'/api/jobs/{job_id}').json()
        assert record['status'] == 'interrupted' and record['can_resume']
        mock_executor.submit.assert_not_called()
        assert connection.post(f'/api/jobs/{job_id}/resume').status_code == 202
        mock_executor.submit.assert_called_once()


def test_export_modes_and_background_pixels(client):
    import numpy as np
    from PIL import Image
    from server.pipeline import run_media
    job = client.post("/api/jobs", files={"file": ("test.mp4", b"x")}).json()
    folder = module.DATA / job["id"]
    (folder / "frames").mkdir()
    (folder / "alpha").mkdir()
    for index in range(3):
        Image.new("RGB", (64, 64), (150, 120, 100)).save(folder / f"frames/{index:06d}.png")
        alpha = np.zeros((64, 64), np.uint8)
        alpha[16:48, 16:48] = 255
        Image.fromarray(alpha).save(folder / f"alpha/{index:06d}.png")
    module.save(job["id"], status="done", metadata={"width": 64, "height": 64, "fps": 30, "frames": 3})
    base = f"/api/jobs/{job['id']}"
    assert client.post(base + "/export", json={"mode": "transparent"}).json()["download"].endswith("/download")
    for mode in ["custom", "matte"]:
        response = client.post(base + "/export", json={"mode": mode, "color": "#00ff00"})
        assert response.status_code == 200, response.text
        links = response.json()
        assert client.get(links["download"]).headers["content-type"] == "video/mp4"
        format_label = "AlphaMatte" if mode == "matte" else "Background"
        assert f"VideoMatting-{format_label}-{job['id'].replace('-', '')[:12]}.mp4" in client.get(links["download"]).headers["content-disposition"]
        path = folder / ("matte.mp4" if mode == "matte" else "background-00ff00.mp4")
        frame = folder / f"{mode}-test.png"
        run_media(["-i", str(path), "-frames:v", "1", str(frame)], threading.Event())
        image = Image.open(frame).convert("RGB")
        assert image.size == (64, 64)
        corner = image.getpixel((0, 0))
        center = image.getpixel((32, 32))
        if mode == "custom":
            assert corner[1] > 245 and corner[0] < 10 and corner[2] < 10
            assert max(abs(actual-expected) for actual, expected in zip(center, (150, 120, 100))) < 10
        else:
            assert max(corner) < 10 and min(center) > 245


def test_export_rejects_unready_invalid_modes_and_color(client):
    job = client.post("/api/jobs", files={"file": ("test.mp4", b"x")}).json()
    base = f"/api/jobs/{job['id']}"
    assert client.post(base + "/export", json={"mode": "custom"}).status_code == 409
    assert client.post(base + "/export", json={"mode": "unknown"}).status_code == 422
    assert client.post(base + "/export", json={"mode": "custom", "color": "../../x"}).status_code == 422
    assert client.get(base + "/exports/source").status_code == 404


def test_audio_export_preserves_alpha_and_adds_audio(client):
    from server.pipeline import run_media
    from PIL import Image
    job = client.post('/api/jobs', files={'file': ('audio.mp4', b'x')}).json()
    folder = module.DATA / job['id']
    run_media(['-f', 'lavfi', '-i', 'color=size=64x64:rate=10', '-f', 'lavfi', '-i', 'sine=frequency=440',
               '-t', '0.3', '-c:v', 'libx264', '-c:a', 'aac', '-f', 'mp4', str(folder / 'source')], threading.Event())
    Image.new('RGBA', (64, 64), (180, 60, 30, 80)).save(folder / 'rgba.png')
    run_media(['-loop', '1', '-i', str(folder / 'rgba.png'), '-t', '0.3', '-c:v', 'libvpx-vp9', '-lossless', '1',
               '-pix_fmt', 'yuva420p', '-auto-alt-ref', '0', str(folder / 'result.webm')], threading.Event())
    module.save(job['id'], status='done', metadata={'duration': .3})
    response = client.post(f"/api/jobs/{job['id']}/export", json={'mode': 'transparent', 'audio': True})
    assert response.status_code == 200, response.text
    assert client.get(response.json()['download']).status_code == 200
    run_media(['-c:v', 'libvpx-vp9', '-i', str(folder / 'audio-result.webm'), '-frames:v', '1', str(folder / 'verify.png')], threading.Event())
    assert Image.open(folder / 'verify.png').getpixel((0, 0))[3] == 80
    run_media(['-i', str(folder / 'audio-result.webm'), '-map', '0:a:0', str(folder / 'verify.wav')], threading.Event())
    assert (folder / 'verify.wav').stat().st_size > 100
