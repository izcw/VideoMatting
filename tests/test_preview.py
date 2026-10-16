import ctypes
import io
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from PIL import Image

from server.preview import publish_preview, read_preview


def test_concurrent_read_write_returns_complete_png(tmp_path):
    image = Image.new('RGBA', (48, 48), (30, 50, 80, 127))
    assert publish_preview(tmp_path, image)

    def read():
        for _ in range(100):
            with Image.open(io.BytesIO(read_preview(tmp_path))) as loaded:
                loaded.load()
                assert loaded.getpixel((0, 0)) == (30, 50, 80, 127)

    def write():
        for _ in range(100):
            assert publish_preview(tmp_path, image)

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(read) for _ in range(4)] + [executor.submit(write)]
        for future in futures:
            future.result()
    assert not (tmp_path / 'preview.tmp').exists()


def test_permission_error_is_retried_then_skipped(tmp_path, monkeypatch, caplog):
    image = Image.new('RGBA', (8, 8))
    assert publish_preview(tmp_path, image)
    old = read_preview(tmp_path)
    calls = []

    def denied(*args):
        calls.append(1)
        raise PermissionError('locked')

    monkeypatch.setattr(Path, 'replace', denied)
    assert publish_preview(tmp_path, image) is False
    assert len(calls) == 3
    assert read_preview(tmp_path) == old
    assert not (tmp_path / 'preview.tmp').exists()
    assert '继续处理' in caplog.text


def test_transient_lock_recovers(tmp_path, monkeypatch):
    replace = Path.replace
    calls = []

    def transient(path, target):
        calls.append(1)
        if len(calls) == 1:
            raise PermissionError('temporary lock')
        return replace(path, target)

    monkeypatch.setattr(Path, 'replace', transient)
    assert publish_preview(tmp_path, Image.new('RGBA', (8, 8)))
    assert len(calls) == 2


def test_preview_save_failure_does_not_abort(tmp_path, monkeypatch):
    image = Image.new('RGBA', (8, 8))

    def failed(*args, **kwargs):
        raise OSError('preview unavailable')

    monkeypatch.setattr(image, 'save', failed)
    assert publish_preview(tmp_path, image) is False


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows 文件共享锁回归测试')
def test_windows_locked_destination_preserves_preview_and_can_recover(tmp_path):
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    image = Image.new('RGBA', (8, 8), 'red')
    assert publish_preview(tmp_path, image)
    # 允许读取但不允许删除/替换，复现 FileResponse 在 Windows 上的占用。
    handle = kernel.CreateFileW(str(tmp_path / 'preview.png'), 0x80000000, 1, None, 3, 0, None)
    assert handle != ctypes.c_void_p(-1).value
    try:
        assert publish_preview(tmp_path, Image.new('RGBA', (8, 8), 'blue')) is False
        assert read_preview(tmp_path)
    finally:
        kernel.CloseHandle(handle)
    assert publish_preview(tmp_path, Image.new('RGBA', (8, 8), 'blue'))
