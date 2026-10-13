import threading
import numpy as np
import pytest
from PIL import Image
from server.pipeline import Cancelled, inspect_video, largest_person, matanyone_prompt, run_media, validate_alpha


def test_largest_person_ignores_small_false_positive():
    mask = np.zeros((50, 50), np.uint8)
    mask[10:40, 10:40] = 1
    mask[1:3, 1:3] = 1
    result = largest_person(mask)
    assert result.sum() == 900 and result[1, 1] == 0


def test_matanyone_input_range_is_255_not_one():
    mask = np.array([[0, 1], [1, 0]], np.uint8)
    prompt = matanyone_prompt(mask)
    assert prompt.dtype == np.float32
    assert prompt.tolist() == [[0, 255], [255, 0]]


def test_quality_guard_rejects_missing_body_and_invalid_alpha():
    prompt = np.ones((3,3), np.uint8)
    validate_alpha(np.ones((3,3), np.float32), prompt)
    with pytest.raises(ValueError):
        validate_alpha(np.zeros((3,3), np.float32), prompt)
    with pytest.raises(ValueError):
        validate_alpha(np.full((3,3), np.nan), prompt)
    with pytest.raises(ValueError):
        validate_alpha(np.ones((1,1), np.float32), prompt)


def test_missing_person_and_invalid_video(tmp_path):
    with pytest.raises(ValueError):
        largest_person(np.zeros((50, 50), np.uint8))
    with pytest.raises(ValueError):
        inspect_video(tmp_path / "nonexistent")


@pytest.mark.parametrize("width,height", [(3840,2160), (4096,2160), (2160,4096)])
def test_accepts_uhd_and_dci_4k(monkeypatch, tmp_path, width, height):
    from unittest.mock import Mock
    import cv2
    capture = Mock()
    capture.isOpened.return_value = True
    info = {cv2.CAP_PROP_FRAME_WIDTH: width, cv2.CAP_PROP_FRAME_HEIGHT: height,
            cv2.CAP_PROP_FPS: 25, cv2.CAP_PROP_FRAME_COUNT: 100}
    capture.get.side_effect = info.get
    monkeypatch.setattr(cv2, 'VideoCapture', lambda _: capture)
    assert inspect_video(tmp_path / '4k.mp4')['width'] == width
    capture.release.assert_called_once()


@pytest.mark.parametrize("width,height,frames,message", [(7680,4320,100,'视频尺寸'), (4096,2160,1525,'视频时长')])
def test_reports_resolution_and_duration_separately(monkeypatch, tmp_path, width, height, frames, message):
    from unittest.mock import Mock
    import cv2
    capture = Mock()
    capture.isOpened.return_value = True
    info = {cv2.CAP_PROP_FRAME_WIDTH: width, cv2.CAP_PROP_FRAME_HEIGHT: height,
            cv2.CAP_PROP_FPS: 25, cv2.CAP_PROP_FRAME_COUNT: frames}
    capture.get.side_effect = info.get
    monkeypatch.setattr(cv2, 'VideoCapture', lambda _: capture)
    with pytest.raises(ValueError, match=message):
        inspect_video(tmp_path / 'over-limit.mp4')


def test_ffmpeg_alpha_merge_keeps_resolution_and_transparency(tmp_path):
    for i in range(3):
        Image.new("RGB", (64, 64), (150, 120, 100)).save(tmp_path / f"color-{i:02d}.png")
        mask = np.zeros((64, 64), np.uint8)
        mask[16:48, 16:48] = 255
        Image.fromarray(mask).save(tmp_path / f"mask-{i:02d}.png")
    output = tmp_path / "out.webm"
    event = threading.Event()
    run_media(["-framerate", "30", "-i", str(tmp_path / "color-%02d.png"),
               "-framerate", "30", "-i", str(tmp_path / "mask-%02d.png"),
               "-filter_complex", "[0:v]format=yuva420p[c];[1:v]format=gray[a];[c][a]alphamerge[v]",
               "-map", "[v]", "-c:v", "libvpx-vp9", "-lossless", "1", "-pix_fmt", "yuva420p",
               "-auto-alt-ref", "0", str(output)], event)
    run_media(["-c:v", "libvpx-vp9", "-i", str(output), "-frames:v", "1", str(tmp_path / "frame.png")], event)
    image = Image.open(tmp_path / "frame.png")
    assert image.size == (64, 64)
    assert image.getpixel((0, 0))[3] == 0
    assert image.getpixel((32, 32))[3] == 255


def test_cancel_terminates_media_process(tmp_path):
    event = threading.Event()
    event.set()
    with pytest.raises(Cancelled):
        run_media(["-f", "lavfi", "-i", "color=size=64x64", "-t", "60",
                   "-c:v", "libx264", str(tmp_path / "long.mp4")], event)
