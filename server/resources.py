"""轻量监控与可取消的内存背压；不降低分辨率或推理精度。"""
import shutil
import subprocess
import threading
import time
import psutil

RESOURCE_LIMIT = 95
MEMORY_LIMIT = RESOURCE_LIMIT
_gpu_lock = threading.Lock()
_gpu_checked = float('-inf')
_gpu_sample = None


def gpu_snapshot():
    """GPU 采样最多每秒一次，供监控和保护共用，避免频繁启动外部程序。"""
    global _gpu_checked, _gpu_sample
    with _gpu_lock:
        now = time.monotonic()
        if now - _gpu_checked < 1:
            return _gpu_sample
        _gpu_sample = query_gpu()
        _gpu_checked = time.monotonic()
        return _gpu_sample


def query_gpu():
    gpu = None
    executable = shutil.which('nvidia-smi')
    if executable:
        try:
            line = subprocess.check_output([executable, '--query-gpu=name,utilization.gpu,memory.used,memory.total',
                '--format=csv,noheader,nounits'], timeout=2,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).decode().splitlines()[0]
            name, usage, used, total = [s.strip() for s in line.split(',')]
            gpu = dict(name=name, usage=float(usage), used_mb=float(used), total_mb=float(total))
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return gpu


def snapshot():
    memory = psutil.virtual_memory()
    return dict(cpu=psutil.cpu_percent(), memory=memory.percent,
                available_gb=round(memory.available / 2**30, 2), total_gb=round(memory.total / 2**30, 2), gpu=gpu_snapshot())


def wait_utilization(cancel, progress, device):
    from .pipeline import Cancelled
    waited = False
    while True:
        cpu = psutil.cpu_percent()
        gpu = gpu_snapshot() if device == 'cuda' else None
        gpu_usage = (gpu or {}).get('usage')
        if cpu >= RESOURCE_LIMIT:
            stage = f'CPU 保护：当前使用 {cpu:.1f}%，降至 {RESOURCE_LIMIT}% 以下后自动继续'
        elif gpu_usage is not None and gpu_usage >= RESOURCE_LIMIT:
            stage = f'GPU 保护：当前使用 {gpu_usage:.1f}%，降至 {RESOURCE_LIMIT}% 以下后自动继续'
        else:
            if waited:
                progress(memory_wait=False, stage='计算资源已释放，继续处理')
            return
        waited = True
        progress(memory_wait=True, stage=stage)
        if cancel.wait(1):
            raise Cancelled()


def wait_memory(cancel, progress, required=2 * 2**30):
    from .pipeline import Cancelled
    waited = False
    while True:
        memory = psutil.virtual_memory()
        if memory.percent < MEMORY_LIMIT:
            if waited:
                progress(memory_wait=False, stage='内存已释放，继续处理')
            return
        waited = True
        progress(memory_wait=True, stage=f'内存保护：当前使用 {memory.percent:.1f}%，降至 {MEMORY_LIMIT}% 以下后自动继续')
        if cancel.wait(2):
            raise Cancelled()


def wait_compute_memory(cancel, progress, required, device):
    # 保留 required 参数以兼容调用；内存和显存都按占用率保护，不按估算分配量等待。
    wait_utilization(cancel, progress, device)
    wait_memory(cancel, progress)
    if device != 'cuda':
        return
    import torch
    from .pipeline import Cancelled
    waiting = False
    while True:
        free, total = torch.cuda.mem_get_info()
        usage = (total - free) * 100 / total
        if usage >= MEMORY_LIMIT:
            # 释放本进程闲置缓存再判断，避免被自身可回收缓存一直阻塞。
            torch.cuda.empty_cache()
            free, total = torch.cuda.mem_get_info()
            usage = (total - free) * 100 / total
        if usage < MEMORY_LIMIT:
            if waiting:
                progress(memory_wait=False, stage='显存已释放，继续处理')
            return
        waiting = True
        progress(memory_wait=True, stage=f'显存保护：当前使用 {usage:.1f}%，降至 {MEMORY_LIMIT}% 以下后自动继续；可关闭其他 GPU 程序')
        if cancel.wait(2):
            raise Cancelled()
