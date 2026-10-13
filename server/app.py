from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, Field

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .pipeline import ROOT, Cancelled, engine, run_media

DATA = ROOT / "data" / "jobs"
DATA.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_BYTES = 250 * 1024 * 1024
logger = logging.getLogger("video-matting")
jobs: dict[str, dict] = {}
cancel_events: dict[str, threading.Event] = {}
lock = threading.RLock()
executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="matting")
export_lock = threading.Lock()

class ExportOptions(BaseModel):
    mode: Literal["transparent", "custom", "matte"] = "transparent"
    color: str = Field(default="#00b140", pattern=r"^#[0-9a-fA-F]{6}$")


def save(job_id: str, **changes):
    with lock:
        jobs[job_id].update(changes)
        jobs[job_id]["updated_at"] = time.time()
        record = DATA / job_id / "job.json"
        temporary = record.with_suffix(".tmp")
        temporary.write_text(json.dumps(jobs[job_id], ensure_ascii=False), encoding="utf-8")
        temporary.replace(record)


def execute(job_id: str):
    cancel = cancel_events[job_id]
    if cancel.is_set():
        return
    started = time.time()
    save(job_id, status="processing", started_at=started, stage="开始处理")
    try:
        metadata = engine.process(DATA / job_id, lambda **kw: save(job_id, **kw), cancel)
        if cancel.is_set():
            raise Cancelled()
        save(job_id, status="done", progress=100, stage="处理完成", metadata=metadata,
             elapsed_seconds=round(time.time() - started, 1))
    except Cancelled:
        save(job_id, status="cancelled", stage="已取消", elapsed_seconds=round(time.time() - started, 1))
    except Exception as error:
        logger.exception("处理失败 %s", job_id)
        save(job_id, status="failed", stage="处理失败", error=str(error),
             elapsed_seconds=round(time.time() - started, 1))


@asynccontextmanager
async def lifespan(_app):
    # 重启后不把中断任务假报成完成；文件留存，用户可重新上传重试。
    for file in DATA.glob("*/job.json"):
        try:
            job = json.loads(file.read_text(encoding="utf-8"))
            if job["status"] in {"queued", "processing"}:
                job.update(status="failed", stage="任务因服务重启中断", error="请重新上传视频重试。")
                file.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
            jobs[job["id"]] = job
        except (ValueError, KeyError):
            logger.warning("忽略损坏任务记录 %s", file.parent.name)
    yield
    for event in cancel_events.values():
        event.set()
    executor.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="VideoMatting", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])


@app.middleware("http")
async def local_write_guard(request: Request, call_next):
    # 无账号的本地工具拒绝跨站写入，不能被网页利用来上传或耗尽本机算力。
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "只允许本地页面操作。"}, status_code=403)
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
        return dict(jobs[job_id])


@app.get("/api/health")
def health():
    import torch
    from .pipeline import MODEL_PATH
    ready = MODEL_PATH.is_file() and (ROOT / "models/sam2.1_hiera_tiny.pt").is_file() and (ROOT / "vendor/SAM2/sam2").is_dir()
    return {"service": "py-VideoMatting", "ready": ready,
            "device": "CUDA GPU" if torch.cuda.is_available() else "CPU（处理较慢）",
            "model": "MatAnyone 2", "version": "0.1.0"}


@app.get("/api/jobs")
def list_jobs():
    with lock:
        return sorted((dict(j) for j in jobs.values() if not j.get("history_hidden")), key=lambda j: j["created_at"], reverse=True)[:30]


@app.post("/api/history/clear")
def clear_history():
    # 只隐藏历史入口，保留任务元数据及全部媒体；当前预览和下载仍可使用。
    with lock:
        if any(j["status"] in {"uploading", "queued", "processing"} for j in jobs.values()):
            raise HTTPException(409, "还有任务正在上传、排队或处理，请完成或取消后再清空。")
        selected = [job_id for job_id, j in jobs.items() if not j.get("history_hidden")]
        for job_id in selected:
            save(job_id, history_hidden=True)
    return {"cleared": len(selected), "files_deleted": False}


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
        raise
    finally:
        await file.close()
    cancel_events[job_id] = threading.Event()
    save(job_id, status="queued", stage="等待处理", bytes=size)
    executor.submit(execute, job_id)
    return lookup(job_id)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
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


DIST = ROOT / "web" / "dist"
if DIST.is_dir():
    app.mount("/", StaticFiles(directory=DIST, html=True), name="web")
