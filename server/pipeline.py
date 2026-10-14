from __future__ import annotations

import os
import sys
import time
import subprocess
import shutil
import threading
import json
from pathlib import Path
from typing import Callable

import cv2
import numpy as np
from PIL import Image
import imageio_ffmpeg

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("TORCH_HOME", str(ROOT / "models" / "torch"))
os.environ.setdefault("HF_HOME", str(ROOT / "models" / "huggingface"))
MODEL_PATH = ROOT / "models" / "matanyone2.pth"
Progress = Callable[..., None]


class Cancelled(Exception):
    pass


def checkpoint(folder, processor, warmup, completed):
    import torch
    state = {key: value for key, value in vars(processor).items()
             if key not in {'network', 'image_feature_store'}}
    # 预热时复用的静态图像特征可由原帧精确重算，不必每次写入数 GB 检查点。
    # 时序状态仍完整保存；普通帧检查点保持原有语义。
    features = {} if completed == 0 else processor.image_feature_store._store
    payload = dict(version=1, state=state, features=features,
                   warmup=warmup, completed=completed)
    temporary = folder / 'checkpoint.tmp'
    torch.save(payload, temporary)
    temporary.replace(folder / 'checkpoint.pt')


def restore_checkpoint(folder, processor, device):
    import torch
    path = folder / 'checkpoint.pt'
    if not path.exists():
        return -1, 0
    # 仅加载本程序写入的任务目录检查点，不接受用户上传的 pickle。
    saved = torch.load(path, map_location=device, weights_only=False)
    if saved['version'] != 1:
        raise ValueError('检查点版本不兼容')
    processor.__dict__.update(saved['state'])
    processor.device = torch.device(device)
    processor.image_feature_store._store = saved['features']
    return saved['warmup'], saved['completed']


def ffmpeg() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def run_media(args: list[str], cancel: threading.Event, report=None, duration=0) -> None:
    """日志写到临时文件而非 PIPE，避免长视频填满缓冲导致死锁。"""
    import tempfile
    with tempfile.TemporaryFile() as log, tempfile.TemporaryDirectory() as temporary:
        progress_file = Path(temporary) / "ffmpeg-progress.txt"
        process = subprocess.Popen([ffmpeg(), "-hide_banner", "-nostdin", "-y", "-progress", str(progress_file), *args],
                                   stdout=subprocess.DEVNULL, stderr=log,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            while process.poll() is None:
                if cancel.wait(0.15):
                    raise Cancelled()
                if report and duration and progress_file.exists():
                    entries = progress_file.read_text(errors="replace").splitlines()
                    times = [line.split("=", 1)[1] for line in entries if line.startswith("out_time_us=")]
                    if times and times[-1].lstrip("-").isdigit():
                        report(min(1, max(0, int(times[-1]) / 1_000_000 / duration)))
            if process.returncode:
                log.seek(0)
                raise RuntimeError("视频解码或导出失败：" + log.read().decode("utf-8", "replace")[-1400:])
            if report:
                report(1)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def inspect_video(path: Path) -> dict:
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            raise ValueError("文件不是可解码的视频。")
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        if not width or not height or not 1 <= fps <= 120 or frames <= 0:
            raise ValueError("视频尺寸、帧率或帧数无效。")
        duration = frames / fps
        if duration > 60:
            raise ValueError(f"视频时长 {duration:.2f} 秒，超过 60 秒上限，请先裁剪。")
        if width * height > 4096 * 2160:
            raise ValueError(f"视频尺寸 {width}×{height} 超过 4K 像素量上限（4096×2160，竖屏也支持），请先缩小尺寸。")
        return dict(width=width, height=height, fps=fps, frames=frames, duration=duration)
    finally:
        capture.release()


def largest_person(mask: np.ndarray) -> np.ndarray:
    """单人物场景保留最大连通区域，剔除背景中误识别的小人像。"""
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), 8)
    if count < 2:
        raise ValueError("首帧未识别到人物。请使用人物清晰、完整出现的单人物视频。")
    selected = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    result = (labels == selected).astype(np.uint8)
    if result.mean() < 0.005:
        raise ValueError("人物区域过小，无法可靠抠像。")
    return result


def matanyone_prompt(mask: np.ndarray) -> np.ndarray:
    # 官方 InferenceCore 的 matting 分支会自行除以 255，输入必须是 0–255，不能是 0–1。
    return (np.clip(mask, 0, 1) * 255).astype(np.float32)


def validate_alpha(alpha: np.ndarray, prompt: np.ndarray) -> None:
    if alpha.shape != prompt.shape or not np.isfinite(alpha).all():
        raise ValueError("模型返回的 Alpha 尺寸或数值无效。")
    if alpha.sum() < prompt.sum() * .5:
        raise ValueError("人物区域大幅丢失，未导出不可靠结果。请使用人物完整、背景简洁的单人物素材。")


class MattingEngine:
    def __init__(self):
        self.segmenter = None
        self.segment_transform = None
        self.model = None
        self.config = None
        self.refiner = None

    def load(self, progress: Progress, selectors=True):
        import torch
        from torchvision.models.segmentation import (
            DeepLabV3_MobileNet_V3_Large_Weights, deeplabv3_mobilenet_v3_large,
        )
        device = "cuda" if torch.cuda.is_available() else "cpu"
        torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
        if selectors and self.segmenter is None:
            progress(stage="加载自动人物识别模型", progress=12)
            weights = DeepLabV3_MobileNet_V3_Large_Weights.DEFAULT
            self.segment_transform = weights.transforms()
            self.segmenter = deeplabv3_mobilenet_v3_large(weights=weights).to(device).eval()
        if self.model is None:
            source = ROOT / "vendor" / "MatAnyone2"
            if not source.is_dir() or not MODEL_PATH.is_file():
                raise RuntimeError("MatAnyone 2 未准备完成。源码请重新运行「一键启动.cmd」；绿色版请完整解压。")
            progress(stage="加载 MatAnyone 2 精细抠像模型", progress=16)
            sys.path.insert(0, str(source))
            from hydra import compose, initialize_config_dir
            from omegaconf import open_dict
            from matanyone2.model.matanyone2 import MatAnyone2
            with initialize_config_dir(version_base="1.3", config_dir=str(source / "matanyone2/config")):
                cfg = compose(config_name="eval_matanyone_config")
            with open_dict(cfg):
                cfg.model.pretrained_resnet = False
                cfg.max_internal_size = -1  # 精细抠像保持原像素，不暗中降低处理分辨率。
            model = MatAnyone2(cfg, single_object=True).to(device).eval()
            model.load_weights(torch.load(MODEL_PATH, map_location=device, weights_only=True))
            self.model, self.config = model, cfg
        if selectors and self.refiner is None:
            progress(stage="加载 SAM 2 首帧精细分割模型", progress=18)
            sys.path.insert(0, str(ROOT / "vendor" / "SAM2"))
            from sam2.build_sam import build_sam2
            from sam2.sam2_image_predictor import SAM2ImagePredictor
            sam_model = build_sam2("configs/sam2.1/sam2.1_hiera_t.yaml",
                                   str(ROOT / "models/sam2.1_hiera_tiny.pt"),
                                   device=device, apply_postprocessing=False)
            self.refiner = SAM2ImagePredictor(sam_model)
        return device

    def first_mask(self, first: Image.Image, device: str) -> np.ndarray:
        import torch
        with torch.inference_mode():
            scores = self.segmenter(self.segment_transform(first).unsqueeze(0).to(device))["out"]
            categories = scores.argmax(1)[0].cpu().numpy()
            coarse = cv2.resize(largest_person(categories == 15), first.size, interpolation=cv2.INTER_NEAREST)
            ys, xs = np.nonzero(coarse)
            width, height = first.size
            # 自动定位只用于给 SAM 2 提示；不直接将衣服漏识别的语义遮罩当作最终输入。
            pad_x = max(16, (xs.max()-xs.min()) * .25)
            # 长裙可能被语义定位器漏掉，竖向提示保留完整画幅，交给 SAM 判别裙摆。
            # 不用点提示将“人体”错误细化成“头部”子对象。
            box = np.array([max(0, xs.min()-pad_x), 0,
                            min(width-1, xs.max()+pad_x), height-1], dtype=np.float32)
            self.refiner.set_image(np.asarray(first).copy())
            masks, scores, _ = self.refiner.predict(box=box, multimask_output=True)
            masks = masks > 0
            coverage = (masks & coarse.astype(bool)).sum(axis=(1,2)) / max(1, coarse.sum())
            valid = np.flatnonzero((coverage > .85) & (scores > .7))
            if not len(valid):
                raise ValueError("首帧自动选择不可靠，请使用单人物、背景简洁且人物完整的视频。")
            chosen = valid[np.argmax(scores[valid])]
            return largest_person(masks[chosen])

    def process(self, folder: Path, progress: Progress, cancel: threading.Event) -> dict:
        import torch
        from .resources import wait_memory, wait_compute_memory
        source = folder / "source"
        metadata = inspect_video(source)
        required_space = metadata["width"] * metadata["height"] * metadata["frames"] * 5
        if shutil.disk_usage(folder).free < required_space:
            raise ValueError("磁盘剩余空间不足以保存高清原帧和遮罩母版，请清理旧任务后重试。")
        progress(stage="解码原视频，保留原始像素", progress=3, metadata=metadata)
        frames_dir, alpha_dir = folder / "frames", folder / "alpha"
        frames_dir.mkdir(exist_ok=True)
        alpha_dir.mkdir(exist_ok=True)
        rate = str(metadata["fps"])
        # 兼容升级前的完整解码缓存；不信任只有部分帧的目录。
        if not (folder / 'decoded.json').exists() and all((frames_dir / f'{i:06d}.png').is_file() for i in range(metadata['frames'])):
            try:
                with Image.open(frames_dir / f"{metadata['frames'] - 1:06d}.png") as last:
                    last.verify()
                (folder / 'decoded.json').write_text(json.dumps(metadata))
            except (OSError, ValueError):
                pass  # 中断解码留下的半帧不能作为完整缓存。
        if not (folder / 'decoded.json').exists():
            run_media(["-i", str(source), "-an", "-r", rate, "-start_number", "0", '-compression_level', '1',
                       str(frames_dir / "%06d.png")], cancel,
                      lambda value: progress(progress=3 + 5 * value), metadata["duration"])
            (folder / 'decoded.json').write_text(json.dumps(metadata))
        frames = sorted(frames_dir.glob("*.png"))
        if not frames:
            raise ValueError("原视频没有有效画面。")
        progress(stage="准备浏览器原片预览", progress=8)
        if not (folder / 'preview-ready').exists():
            run_media(["-framerate", rate, "-i", str(frames_dir / "%06d.png"), '-i', str(source),
                   '-map', '0:v:0', '-map', '1:a:0?', '-c:a', 'aac',
                   "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p",
                   '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2',
                   "-movflags", "+faststart", str(folder / "original-preview.mp4")], cancel,
                  lambda value: progress(progress=8 + 3 * value), metadata["duration"])
            (folder / 'preview-ready').touch()
        progress(original_ready=True)
        if (folder / 'alpha-ready').exists() and all((alpha_dir / f'{i:06d}.png').is_file() for i in range(len(frames))):
            return self.encode(folder, metadata, len(frames), progress, cancel)
        wait_memory(cancel, progress)
        device = self.load(progress, selectors=not (folder / 'first-mask.png').exists())
        if cancel.is_set():
            raise Cancelled()
        from matanyone2.inference.inference_core import InferenceCore
        import matanyone2.inference.memory_manager as memory_module
        from matanyone2.model.utils.memory_utils import get_similarity
        from .inference_memory import bounded_similarity
        memory_module.get_similarity = bounded_similarity(get_similarity,
            lambda required: wait_compute_memory(cancel, progress, max(2 * 2**30, required), device))
        processor = InferenceCore(self.model, cfg=self.config)
        warmup_done, completed = restore_checkpoint(folder, processor, device)
        first = Image.open(frames[0]).convert("RGB")
        progress(stage="自动选中首帧人物", progress=20, device=device)
        with torch.inference_mode():
            mask_path = folder / 'first-mask.png'
            if mask_path.exists():
                mask = (np.asarray(Image.open(mask_path).convert('L')) > 127).astype(np.uint8)
            else:
                mask = self.first_mask(first, device)
                Image.fromarray(mask * 255).save(mask_path)
            mask_tensor = torch.from_numpy(matanyone_prompt(mask)).to(device)
            first_tensor = torch.from_numpy(np.array(first).copy()).permute(2, 0, 1).float().to(device) / 255
            progress(stage="首帧预热，稳定细节", progress=22)
            # 步骤前检查占用率，不要求固定 GB 预留，也不降低画质。
            if warmup_done < 0:
                wait_compute_memory(cancel, progress, 0, device)
                processor.step(first_tensor, mask_tensor, objects=[1], delete_buffer=False)
                checkpoint(folder, processor, 0, 0)
            for warmup in range(max(0, warmup_done), 10):
                if cancel.is_set():
                    raise Cancelled()
                wait_compute_memory(cancel, progress, 0, device)
                processor.step(first_tensor, first_frame_pred=True, delete_buffer=False)
                checkpoint(folder, processor, warmup + 1, 0)
                progress(stage=f"首帧预热（{warmup+1}/10）", progress=22+(warmup+1)*.2)
            frame_times = []
            for index in range(completed, len(frames)):
                frame_path = frames[index]
                if cancel.is_set():
                    raise Cancelled()
                wait_compute_memory(cancel, progress, 0, device)
                frame_started = time.monotonic()
                image = np.asarray(Image.open(frame_path).convert("RGB")).copy()
                tensor = torch.from_numpy(image).permute(2, 0, 1).float().to(device) / 255
                prediction = processor.step(tensor, first_frame_pred=index == 0)
                alpha = processor.output_prob_to_mask(prediction).cpu().numpy()
                validate_alpha(alpha, mask)
                Image.fromarray(np.round(np.clip(alpha, 0, 1) * 255).astype(np.uint8)).save(
                    alpha_dir / f"{index:06d}.png", compress_level=1)
                # 定期落盘完整时序状态，恢复最多重做最近 4 帧，不跳帧或减少预热。
                if (index + 1) % 5 == 0 or index + 1 == len(frames):
                    checkpoint(folder, processor, 10, index + 1)
                if index == completed or index % 5 == 0:
                    preview = Image.fromarray(image).convert('RGBA')
                    preview.putalpha(Image.fromarray(np.round(np.clip(alpha, 0, 1) * 255).astype(np.uint8)))
                    preview.thumbnail((720, 720))  # 仅进度预览缩略图，母版不缩小。
                    preview.save(folder / 'preview.tmp', format='PNG', compress_level=1)
                    (folder / 'preview.tmp').replace(folder / 'preview.png')
                frame_times.append(time.monotonic() - frame_started)
                progress(stage="逐帧精细抠像", progress=25 + 60 * (index + 1) / len(frames),
                         completed_frames=index + 1, total_frames=len(frames), preview_frame=index + 1,
                         eta_seconds=round(np.mean(frame_times[-10:]) * (len(frames) - index - 1)), memory_wait=False)
        del processor
        (folder / 'alpha-ready').touch()
        return self.encode(folder, metadata, len(frames), progress, cancel, device)

    def encode(self, folder, metadata, frame_count, progress, cancel, device='cached'):
        frames_dir, alpha_dir = folder / 'frames', folder / 'alpha'
        rate = str(metadata['fps'])
        progress(stage="合成高质量透明 WebM", progress=88)
        output = folder / "result.webm"
        run_media(["-framerate", rate, "-i", str(frames_dir / "%06d.png"),
                   "-framerate", rate, "-i", str(alpha_dir / "%06d.png"),
                   "-filter_complex", "[0:v]format=yuva420p[c];[1:v]format=gray[a];[c][a]alphamerge=shortest=1[v]",
                   "-map", "[v]", "-an", "-c:v", "libvpx-vp9", "-crf", "16", "-b:v", "0",
                   "-pix_fmt", "yuva420p", "-auto-alt-ref", "0", '-row-mt', '1', str(output)], cancel,
                  lambda value: progress(progress=88 + 11 * value), metadata["duration"])
        # 母版保留原片、PNG 和参数；输出不靠绿色预览视频再次转码。
        return {**metadata, "frames": frame_count, "size": output.stat().st_size,
                "model": "MatAnyone 2", "selector": "DeepLabV3 MobileNet + SAM 2.1", "device": device}


engine = MattingEngine()
