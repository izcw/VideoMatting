from __future__ import annotations

import json
import logging
import threading
import time
import uuid
import math
import shutil
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, Field

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .pipeline import ROOT, Cancelled, engine, run_media
from .network import allowed_hosts, lan_addresses
from .preview import read_preview

DATA = ROOT / "data" / "jobs"
DATA.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_BYTES = 250 * 1024 * 1024
logger = logging.getLogger("video-matting")
jobs: dict[str, dict] = {}
cancel_events: dict[str, threading.Event] = {}
lock = threading.RLock()
executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="matting")
export_lock = threading.Lock()
HISTORY_LIMIT = 20
VERSION = "1.1.1"


def stored_elapsed(job: dict) -> float:
    if job.get('elapsed_seconds') is not None:
        return job['elapsed_seconds']
    start = job.get('started_at') or job.get('created_at', 0)
    return round(max(0, job.get('updated_at', start) - start), 1)


def remove_job_files(job_id: str):
    # 验证绝对路径，仅删除数据目录内的 UUID 子目录。
    if str(uuid.UUID(job_id)) != job_id:
        raise HTTPException(400, '无效任务路径')
    folder = DATA / job_id
    if folder.resolve().parent != DATA.resolve() or folder.is_symlink() or folder.is_junction():
        raise HTTPException(400, '任务路径异常，未删除文件')
    try:
        if folder.exists():
            shutil.rmtree(folder)
    except OSError as error:
        raise HTTPException(409, '文件正在使用或无法删除，请停止预览或下载后重试。') from error
    jobs.pop(job_id, None)
    cancel_events.pop(job_id, None)


def prune_history():
    with lock:
        visible = sorted((j for j in jobs.values() if not j.get('history_hidden')),
                         key=lambda j: j['created_at'], reverse=True)
        for job in visible[HISTORY_LIMIT:]:
            save(job['id'], history_hidden=True)


def public_job(job: dict) -> dict:
    return {**job, 'elapsed_seconds': stored_elapsed(job), 'can_resume': not job.get('resume_blocked') and job['status'] in {'interrupted', 'failed', 'cancelled'}
            and (DATA / job['id'] / 'source').is_file()}

class ExportOptions(BaseModel):
    mode: Literal["transparent", "custom", "matte"] = "transparent"
    color: str = Field(default="#00b140", pattern=r"^#[0-9a-fA-F]{6}$")
    audio: bool = False


def save(job_id: str, **changes):
    with lock:
        jobs[job_id].update(changes)
        if jobs[job_id]['status'] == 'processing' and jobs[job_id].get('started_at'):
            jobs[job_id]['elapsed_seconds'] = round(time.time() - jobs[job_id]['started_at'], 1)
        jobs[job_id]["updated_at"] = time.time()
        record = DATA / job_id / "job.json"
        temporary = record.with_suffix(".tmp")
        temporary.write_text(json.dumps(jobs[job_id], ensure_ascii=False), encoding="utf-8")
        temporary.replace(record)
        event = {key: value for key, value in changes.items()
                 if key in {'status', 'stage', 'progress', 'error', 'memory_wait', 'completed_frames', 'total_frames'}}
        if event:
            event.update(time=time.time(), job_id=job_id)
            path = DATA / job_id / 'events.jsonl'
            if path.exists() and path.stat().st_size > 2 * 1024 * 1024:
                path.replace(path.with_suffix('.previous.jsonl'))
            with path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + '\n')


def record_resources(job_id: str, sample: dict):
    """统计任务处理期间的整机占用；缺失的 GPU 数据不当作零。"""
    with lock:
        if job_id not in jobs or jobs[job_id]['status'] != 'processing':
            return
        stats = {key: dict(value) for key, value in jobs[job_id].get('resource_stats', {}).items()}
        values = {'cpu': sample.get('cpu'), 'memory': sample.get('memory'),
                  'gpu': (sample.get('gpu') or {}).get('usage')}
        for key, value in values.items():
            if not isinstance(value, (float, int)) or not math.isfinite(value):
                continue
            value = max(0, min(100, value))
            previous = stats.get(key, {'count': 0, 'sum': 0, 'max': 0})
            count, total = previous['count'] + 1, previous['sum'] + value
            stats[key] = dict(count=count, sum=total, max=max(previous['max'], value), average=total / count)
        save(job_id, resource_stats=stats)


def monitor_task(job_id: str, stop: threading.Event):
    import psutil
    from .resources import snapshot
    psutil.cpu_percent()  # 初始化本线程的 CPU 采样基线。
    delay = 1
    while not stop.wait(delay):
        try:
            record_resources(job_id, snapshot())
        except Exception:
            logger.warning('资源采样暂不可用 %s', job_id, exc_info=True)
        delay = 3


def execute(job_id: str):
    cancel = cancel_events.get(job_id)
    if cancel is None or cancel.is_set():
        return
    started = time.time()
    save(job_id, status="processing", started_at=started, stage="开始处理")
    monitor_stop = threading.Event()
    monitor_thread = threading.Thread(target=monitor_task, args=(job_id, monitor_stop), daemon=True)
    monitor_thread.start()
    try:
        def report(**kw):
            if 'progress' in kw:
                kw['progress'] = max(jobs[job_id].get('progress', 0), kw['progress'])
            save(job_id, **kw)
        metadata = engine.process(DATA / job_id, report, cancel)
        if cancel.is_set():
            raise Cancelled()
        save(job_id, status="done", progress=100, stage="处理完成", metadata=metadata,
             elapsed_seconds=round(time.time() - started, 1))

    except Cancelled:
        save(job_id, status="cancelled", stage="已取消", elapsed_seconds=round(time.time() - started, 1))
    except Exception as error:
        logger.exception("处理失败 %s", job_id)
        (DATA / job_id / 'error.log').write_text(traceback.format_exc(), encoding='utf-8')
        oom = isinstance(error, MemoryError) or any(word in str(error).lower() for word in ('not enough memory', 'out of memory', 'alloc_cpu', 'cannot allocate memory'))
        save(job_id, status="interrupted" if oom else "failed", stage="内存不足，已保留检查点" if oom else "处理失败",
             error="可用内存不足，已保留完成的步骤；关闭其他程序后点击任务恢复。" if oom else str(error),
             elapsed_seconds=round(time.time() - started, 1))
    finally:
        monitor_stop.set()
        monitor_thread.join(timeout=3)


@asynccontextmanager
async def lifespan(_app):
    from .integration import api_token
    api_token()
    # 重启后不把中断任务假报成完成；文件留存，用户可重新上传重试。
    for file in DATA.glob("*/job.json"):
        try:
            job = json.loads(file.read_text(encoding="utf-8"))
            if job["status"] in {"uploading", "queued", "processing"}:
                if job['status'] == 'uploading':
                    job['resume_blocked'] = True  # 上传未完成，原片可能不完整。
                job.update(status="interrupted", elapsed_seconds=stored_elapsed(job), stage="任务已中断，点击可恢复处理", error="服务已停止，可复用原视频重新处理。")
                file.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
            jobs[job["id"]] = job
        except (ValueError, KeyError):
            logger.warning("忽略损坏任务记录 %s", file.parent.name)
    prune_history()
    yield
    for event in cancel_events.values():
        event.set()
    executor.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="VideoMatting", version=VERSION, lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts())


@app.middleware("http")
async def local_write_guard(request: Request, call_next):
    # 无账号的本地工具拒绝跨站写入，不能被网页利用来上传或耗尽本机算力。
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "只允许当前服务页面操作。"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def lookup(job_id: str) -> dict:
    try:
        if str(uuid.UUID(job_id)) != job_id:
            raise ValueError()
    except ValueError:
        raise HTTPException(404, "任务不存在")
    with lock:
        if job_id not in jobs:
            raise HTTPException(404, "任务不存在")
        return public_job(jobs[job_id])


@app.get("/api/health")
def health():
    import torch
    from .pipeline import MODEL_PATH
    ready = MODEL_PATH.is_file() and (ROOT / "models/sam2.1_hiera_tiny.pt").is_file() and (ROOT / "vendor/SAM2/sam2").is_dir()
    return {"service": "py-VideoMatting", "ready": ready,
            "device": "CUDA GPU" if torch.cuda.is_available() else "CPU（处理较慢）",
            "model": "MatAnyone 2", "version": VERSION, "lan_addresses": lan_addresses()}


@app.get("/api/jobs")
def list_jobs():
    with lock:
        return sorted((public_job(j) for j in jobs.values() if not j.get("history_hidden")), key=lambda j: j["created_at"], reverse=True)[:HISTORY_LIMIT]


@app.get('/api/system')
def system_status():
    from .resources import snapshot, MEMORY_LIMIT
    return {**snapshot(), 'device': health()['device'], 'memory_limit': MEMORY_LIMIT}


@app.delete('/api/jobs/{job_id}')
def delete_job(job_id: str):
    with export_lock, lock:
        job = lookup(job_id)
        if job['status'] in {'queued', 'processing', 'uploading'}:
            raise HTTPException(409, '请先取消处理，再删除记录。')
        remove_job_files(job_id)
    return {'deleted': True, 'files_deleted': True}


@app.get('/api/jobs/{job_id}/preview-frame')
def preview_frame(job_id: str):
    lookup(job_id)
    try:
        content = read_preview(DATA / job_id)
    except FileNotFoundError:
        raise HTTPException(404, '首帧尚未生成')
    return Response(content, media_type='image/png', headers={'Cache-Control': 'no-store'})


@app.post("/api/history/clear")
def clear_history():
    # 一并清理旧版本隐藏的历史任务，不触碰用户上传前的原始文件。
    with export_lock, lock:
        if any(j["status"] in {"uploading", "queued", "processing"} for j in jobs.values()):
            raise HTTPException(409, "还有任务正在上传、排队或处理，请完成或取消后再清空。")
        selected = list(jobs)
        for job_id in selected:
            remove_job_files(job_id)
    return {"cleared": len(selected), "files_deleted": True}


@app.post("/api/jobs", status_code=202)
async def upload(file: UploadFile = File(...)):
    if Path(file.filename or "").suffix.lower() not in {".mp4", ".mov", ".webm", ".mkv"}:
        raise HTTPException(400, "请上传 MP4、MOV、WebM 或 MKV 视频。")
    with lock:
        if sum(j["status"] in {"uploading", "queued", "processing"} for j in jobs.values()) >= 3:
            raise HTTPException(429, "队列已满，请等待当前任务完成。")
        job_id = str(uuid.uuid4())
        folder = DATA / job_id
        folder.mkdir()
        jobs[job_id] = {"id": job_id, "name": Path(file.filename).name, "status": "uploading",
                        "progress": 0, "stage": "上传中", "created_at": time.time(), "original_ready": False}
    size = 0
    try:
        with (folder / "source").open("wb") as target:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "视频不能超过 250 MB。")
                target.write(chunk)
        if size == 0:
            raise HTTPException(400, "视频为空。")
    except Exception:
        (folder / "source").unlink(missing_ok=True)
        save(job_id, status="failed", stage="上传失败", error="文件为空、过大或上传中断。")
        prune_history()
        raise
    finally:
        await file.close()
    cancel_events[job_id] = threading.Event()
    save(job_id, status="queued", stage="等待处理", bytes=size)
    prune_history()
    executor.submit(execute, job_id)
    return lookup(job_id)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    return lookup(job_id)


@app.post("/api/jobs/{job_id}/resume", status_code=202)
def resume_job(job_id: str):
    with lock:
        job = lookup(job_id)
        if job['status'] in {'queued', 'processing'}:
            return job  # 刷新页面重复点击不创建第二个推理任务。
        if not job['can_resume']:
            raise HTTPException(409, '任务无法恢复，请重新上传原视频。')
        if sum(j['status'] in {'uploading', 'queued', 'processing'} for j in jobs.values()) >= 3:
            raise HTTPException(429, '队列已满，请等待当前任务完成。')
        cancel_events[job_id] = threading.Event()
        save(job_id, status='queued', stage='恢复处理：读取已保存的步骤与检查点', error=None,
             elapsed_seconds=None, memory_wait=False, eta_seconds=None,
             started_at=None, history_hidden=False, resumed_at=time.time())
        prune_history()
        executor.submit(execute, job_id)
        return lookup(job_id)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    job = lookup(job_id)
    if job["status"] in {"queued", "processing"}:
        cancel_events[job_id].set()
        if job["status"] == "queued":
            save(job_id, status="cancelled", stage="已取消")
        else:
            save(job_id, stage="正在取消，等待当前帧结束")
    return lookup(job_id)


@app.get("/api/jobs/{job_id}/{asset}")
def media(job_id: str, asset: str):
    job = lookup(job_id)
    names = {"original": ("original-preview.mp4", "video/mp4"),
             "result": ("result.webm", "video/webm"), "download": ("result.webm", "video/webm")}
    if asset not in names:
        raise HTTPException(404, "素材不存在")
    name, mime = names[asset]
    path = DATA / job_id / name
    if not path.is_file() or (asset != "original" and job["status"] != "done") or (
        asset == "original" and not job.get("original_ready")):
        raise HTTPException(404, "素材尚未生成")
    filename = f"VideoMatting-WebM-{job_id.replace('-', '')[:12]}.webm" if asset == "download" else None
    return FileResponse(path, media_type=mime, filename=filename)


@app.post("/api/jobs/{job_id}/export")
def export_job(job_id: str, options: ExportOptions):
    job = lookup(job_id)
    if job["status"] != "done":
        raise HTTPException(409, "请等待抠像完成后再导出。")
    if options.audio:
        muted = export_job(job_id, ExportOptions(mode=options.mode, color=options.color))
        folder = DATA / job_id
        base = 'result.webm' if options.mode == 'transparent' else 'matte.mp4' if options.mode == 'matte' else f'background-{options.color[1:].lower()}.mp4'
        output = folder / ('audio-' + base)
        with export_lock:
            if not output.exists():
                temporary = output.with_name(output.stem + '.partial' + output.suffix)
                try:
                    run_media(['-i', str(folder / base), '-i', str(folder / 'source'), '-map', '0:v:0', '-map', '1:a:0?',
                               '-c:v', 'copy', '-c:a', 'libopus' if output.suffix == '.webm' else 'aac',
                               '-t', str(job['metadata']['duration']), str(temporary)], threading.Event())
                    temporary.replace(output)
                except Exception as error:
                    temporary.unlink(missing_ok=True)
                    raise HTTPException(500, f'音频合成失败：{error}')
        return {'preview': muted['preview'], 'download': f'/api/jobs/{job_id}/audio/{output.name}'}
    if options.mode == "transparent":
        return {"preview": f"/api/jobs/{job_id}/result", "download": f"/api/jobs/{job_id}/download"}
    folder = DATA / job_id
    name = "matte.mp4" if options.mode == "matte" else f"background-{options.color[1:].lower()}.mp4"
    output = folder / name
    with export_lock:
        if not output.is_file():
            temporary = folder / (output.stem + ".partial.mp4")
            meta = job["metadata"]
            rate = str(meta["fps"])
            if options.mode == "matte":
                args = ["-framerate", rate, "-i", str(folder / "alpha/%06d.png"), "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2"]
            else:
                args = ["-f", "lavfi", "-i", f"color=c={options.color}:s={meta['width']}x{meta['height']}:r={rate}",
                        "-framerate", rate, "-i", str(folder / "frames/%06d.png"),
                        "-framerate", rate, "-i", str(folder / "alpha/%06d.png"),
                        "-filter_complex", "[1:v]format=rgba[c];[2:v]format=gray[a];[c][a]alphamerge[fg];[0:v][fg]overlay=shortest=1,pad=ceil(iw/2)*2:ceil(ih/2)*2[v]", "-map", "[v]"]
            try:
                run_media([*args, "-frames:v", str(meta["frames"]), "-an", "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temporary)], threading.Event())
                temporary.replace(output)
            except Exception as error:
                temporary.unlink(missing_ok=True)
                raise HTTPException(500, f"导出失败：{error}")
    return {"preview": f"/api/jobs/{job_id}/exports/{output.stem}", "download": f"/api/jobs/{job_id}/exports/{output.stem}?download=true"}


@app.get('/api/jobs/{job_id}/audio/{name}')
def audio_download(job_id: str, name: str):
    import re
    job = lookup(job_id)
    if job['status'] != 'done' or not re.fullmatch(r'audio-(result\.webm|matte\.mp4|background-[0-9a-f]{6}\.mp4)', name):
        raise HTTPException(404)
    path = DATA / job_id / name
    if not path.exists():
        raise HTTPException(404)
    kind = 'WebM' if name.endswith('.webm') else 'AlphaMatte' if 'matte' in name else 'Background'
    return FileResponse(path, filename=f'VideoMatting-{kind}-Audio-{job_id.replace("-", "")[:12]}{path.suffix}')


@app.get("/api/jobs/{job_id}/exports/{name}")
def exported_media(job_id: str, name: str, download: bool = False):
    import re
    job = lookup(job_id)
    if job["status"] != "done" or not re.fullmatch(r"matte|background-[0-9a-f]{6}", name):
        raise HTTPException(404, "导出素材不存在")
    path = DATA / job_id / f"{name}.mp4"
    if not path.is_file():
        raise HTTPException(404, "导出素材尚未生成")
    format_label = "AlphaMatte" if name == "matte" else "Background"
    return FileResponse(path, media_type="video/mp4", filename=f"VideoMatting-{format_label}-{job_id.replace('-', '')[:12]}.mp4" if download else None)


from .integration import install_routes
install_routes(app, sys.modules[__name__])

DIST = ROOT / "web" / "dist"
if DIST.is_dir():
    app.mount("/", StaticFiles(directory=DIST, html=True), name="web")
