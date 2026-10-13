import importlib
import threading
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

module = importlib.import_module("server.app")


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
