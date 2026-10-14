"""轻量监控与可取消的内存背压；不降低分辨率或推理精度。"""
import shutil
import subprocess
import psutil

MEMORY_LIMIT = 95


def snapshot():
    memory = psutil.virtual_memory()
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
    return dict(cpu=psutil.cpu_percent(), memory=memory.percent,
                available_gb=round(memory.available / 2**30, 2), total_gb=round(memory.total / 2**30, 2), gpu=gpu)


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
