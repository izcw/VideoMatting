"""短时读写锁保护预览；网络发送期间不持有 Windows 文件句柄。"""
import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger('video-matting')
_lock = threading.Lock()


def read_preview(folder: Path) -> bytes:
    with _lock:
        return (folder / 'preview.png').read_bytes()


def publish_preview(folder: Path, image) -> bool:
    """预览失败仅跳过缩略图，不中断母版、检查点或最终视频的生成。"""
    temporary = folder / 'preview.tmp'
    try:
        image.save(temporary, format='PNG', compress_level=1)
        for attempt in range(3):
            try:
                with _lock:
                    temporary.replace(folder / 'preview.png')
                return True
            except PermissionError:
                if attempt == 2:
                    raise
                # 杀毒软件等外部读取也可能短暂占用，不长时间阻塞推理。
                time.sleep(0.025)
    except OSError:
        logger.warning('预览更新失败，保留上次画面并继续处理：%s', folder.name, exc_info=True)
        return False
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
