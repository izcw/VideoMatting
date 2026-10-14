import sys
import os
import threading
from types import SimpleNamespace

import numpy as np
import pytest
import torch

from server.pipeline import ROOT, checkpoint, restore_checkpoint, Cancelled
from server.inference_memory import bounded_similarity


@pytest.fixture(autouse=True)
def stable_compute_utilization(monkeypatch):
    from server import resources
    monkeypatch.setattr(resources.psutil, 'cpu_percent', lambda: 25)
    monkeypatch.setattr(resources, 'gpu_snapshot', lambda: {'usage': 25})


@pytest.mark.parametrize('device,kind', [('cpu', 'CPU'), ('cuda', 'GPU')])
def test_compute_utilization_waits_at_95_and_resumes(monkeypatch, device, kind):
    from server import resources
    samples = iter([96, 95, 94.9])
    if device == 'cpu':
        monkeypatch.setattr(resources.psutil, 'cpu_percent', lambda: next(samples))
    else:
        monkeypatch.setattr(resources, 'gpu_snapshot', lambda: {'usage': next(samples)})
    reports = []
    resources.wait_utilization(SimpleNamespace(wait=lambda seconds: False), lambda **kw: reports.append(kw), device)
    assert len(reports) == 3
    assert kind in reports[0]['stage']
    assert reports[1]['memory_wait'] is True
    assert reports[-1]['memory_wait'] is False


def test_compute_utilization_wait_can_cancel(monkeypatch):
    from server import resources
    monkeypatch.setattr(resources.psutil, 'cpu_percent', lambda: 100)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(Cancelled):
        resources.wait_utilization(cancel, lambda **kw: None, 'cpu')


def test_unavailable_gpu_utilization_does_not_block(monkeypatch):
    from server import resources
    monkeypatch.setattr(resources, 'gpu_snapshot', lambda: None)
    reports = []
    resources.wait_utilization(threading.Event(), lambda **kw: reports.append(kw), 'cuda')
    assert not reports


def test_checkpoint_preserves_recurrent_state_and_shared_references(tmp_path):
    tensor = torch.randn(1, 16, 8, 8)
    shared = {'tensor': tensor}
    processor = SimpleNamespace(network=object(), image_feature_store=SimpleNamespace(_store={3: tensor}),
                                memory=shared, object_manager=shared, last_mask=tensor, curr_ti=13)
    checkpoint(tmp_path, processor, 10, 5)
    restored = SimpleNamespace(network='current model', image_feature_store=SimpleNamespace(_store={}))
    assert restore_checkpoint(tmp_path, restored, 'cpu') == (10, 5)
    assert restored.network == 'current model'
    assert restored.memory is restored.object_manager
    assert torch.equal(restored.last_mask, tensor)
    assert torch.equal(restored.image_feature_store._store[3], tensor)
    assert not (tmp_path / 'checkpoint.tmp').exists()


@pytest.mark.parametrize('selection', [True, False])
def test_bounded_similarity_matches_original(selection):
    sys.path.insert(0, str(ROOT / 'vendor/MatAnyone2'))
    from matanyone2.model.utils.memory_utils import get_similarity
    torch.manual_seed(12)
    mk, ms, qk = torch.randn(1, 64, 35), torch.rand(1, 1, 35), torch.randn(1, 64, 601)
    qe = torch.rand_like(qk) if selection else None
    guarded = []
    result = bounded_similarity(get_similarity, guarded.append)(mk, ms, qk, qe)
    torch.testing.assert_close(result, get_similarity(mk, ms, qk, qe), rtol=1e-5, atol=1e-5)
    assert guarded and guarded[0] > result.numel() * result.element_size()
    assert result.stride(1) == 1
    from matanyone2.model.utils.memory_utils import do_softmax
    expected = get_similarity(mk, ms, qk, qe)
    affinity = do_softmax(result, top_k=30, inplace=True)
    torch.testing.assert_close(affinity, do_softmax(expected, top_k=30, inplace=True), rtol=1e-5, atol=1e-5)


def test_memory_backpressure_waits_and_can_cancel(monkeypatch):
    from server import resources
    monkeypatch.setattr(resources.psutil, 'virtual_memory', lambda: SimpleNamespace(percent=95, available=0))
    cancel = threading.Event()
    cancel.set()
    reports = []
    with pytest.raises(Cancelled):
        resources.wait_memory(cancel, lambda **kw: reports.append(kw))
    assert reports[-1]['memory_wait']


def test_memory_recovers_below_95_percent(monkeypatch):
    from server import resources
    samples = iter([SimpleNamespace(percent=96, available=1), SimpleNamespace(percent=95, available=10**10), SimpleNamespace(percent=94.9, available=1)])
    monkeypatch.setattr(resources.psutil, 'virtual_memory', lambda: next(samples))
    reports = []
    resources.wait_memory(SimpleNamespace(wait=lambda seconds: False), lambda **kw: reports.append(kw))
    assert reports[-1]['memory_wait'] is False


def test_large_reserve_does_not_block_below_threshold(monkeypatch):
    from server import resources
    monkeypatch.setattr(resources.psutil, 'virtual_memory', lambda: SimpleNamespace(percent=90, available=2**30))
    reports = []
    resources.wait_memory(threading.Event(), lambda **kw: reports.append(kw), required=20 * 2**30)
    assert reports == []


def test_gpu_large_estimate_does_not_block_8gb_device(monkeypatch):
    from server import resources
    monkeypatch.setattr(resources.psutil, 'virtual_memory', lambda: SimpleNamespace(percent=50))
    monkeypatch.setattr(torch.cuda, 'mem_get_info', lambda: (2 * 2**30, 8 * 2**30))
    reports = []
    resources.wait_compute_memory(threading.Event(), lambda **kw: reports.append(kw), 18.1 * 2**30, 'cuda')
    assert not reports


def test_gpu_waits_at_95_and_resumes_below_threshold(monkeypatch):
    from server import resources
    monkeypatch.setattr(resources.psutil, 'virtual_memory', lambda: SimpleNamespace(percent=50))
    samples = iter([(500, 10000), (500, 10000), (501, 10000)])
    monkeypatch.setattr(torch.cuda, 'mem_get_info', lambda: next(samples))
    released = []
    monkeypatch.setattr(torch.cuda, 'empty_cache', lambda: released.append(True))
    reports = []
    resources.wait_compute_memory(SimpleNamespace(wait=lambda seconds: False), lambda **kw: reports.append(kw), 0, 'cuda')
    assert reports[0]['memory_wait'] is True and '95.0%' in reports[0]['stage']
    assert reports[-1]['memory_wait'] is False
    assert released == [True]


def test_gpu_cache_release_avoids_wait(monkeypatch):
    from server import resources
    monkeypatch.setattr(resources.psutil, 'virtual_memory', lambda: SimpleNamespace(percent=50))
    samples = iter([(400, 10000), (1000, 10000)])
    monkeypatch.setattr(torch.cuda, 'mem_get_info', lambda: next(samples))
    monkeypatch.setattr(torch.cuda, 'empty_cache', lambda: None)
    reports = []
    resources.wait_compute_memory(threading.Event(), lambda **kw: reports.append(kw), 0, 'cuda')
    assert not reports


def test_gpu_wait_is_cancellable(monkeypatch):
    from server import resources
    monkeypatch.setattr(resources.psutil, 'virtual_memory', lambda: SimpleNamespace(percent=50))
    monkeypatch.setattr(torch.cuda, 'mem_get_info', lambda: (100, 10000))
    monkeypatch.setattr(torch.cuda, 'empty_cache', lambda: None)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(Cancelled):
        resources.wait_compute_memory(cancel, lambda **kw: None, 0, 'cuda')


@pytest.mark.skipif(os.environ.get('MATTING_MODEL_TEST') != '1', reason='按需运行真实权重恢复测试')
def test_cached_warmup_is_bit_exact(tmp_path):
    from server.pipeline import MattingEngine
    engine = MattingEngine()
    device = engine.load(lambda **kw: None, selectors=False)
    from matanyone2.inference.inference_core import InferenceCore
    torch.manual_seed(17)
    image = torch.rand(3, 128, 128, device=device)
    mask = torch.zeros(128, 128, device=device)
    mask[20:108, 30:98] = 255
    def run(cached):
        processor = InferenceCore(engine.model, cfg=engine.config)
        outputs = []
        processor.step(image, mask, objects=[1], delete_buffer=not cached)
        for index in range(10):
            outputs.append(processor.step(image, first_frame_pred=True, delete_buffer=not cached).clone())
            if cached and index == 4:
                checkpoint(tmp_path, processor, 5, 0)
                processor.image_feature_store.delete(0)
                processor = InferenceCore(engine.model, cfg=engine.config)
                assert restore_checkpoint(tmp_path, processor, device) == (5, 0)
                assert len(processor.image_feature_store) == 0
        outputs.append(processor.step(image, first_frame_pred=True).clone())
        outputs.append(processor.step(image * .97).clone())
        assert len(processor.image_feature_store) == 0
        return outputs
    with torch.inference_mode():
        expected, actual = run(False), run(True)
    for a, b in zip(expected, actual):
        torch.testing.assert_close(a, b, rtol=0, atol=0)


@pytest.mark.skipif(os.environ.get('MATTING_MODEL_TEST') != '1', reason='按需运行真实权重恢复测试')
def test_real_model_checkpoint_prediction_matches(tmp_path):
    from server.pipeline import MattingEngine
    engine = MattingEngine()
    device = engine.load(lambda **kw: None)
    from matanyone2.inference.inference_core import InferenceCore
    processor = InferenceCore(engine.model, cfg=engine.config)
    torch.manual_seed(7)
    image = torch.rand(3, 128, 128, device=device)
    mask = torch.zeros(128, 128, device=device)
    mask[20:108, 30:98] = 255
    with torch.inference_mode():
        processor.step(image, mask, objects=[1])
        processor.step(image, first_frame_pred=True)
        checkpoint(tmp_path, processor, 1, 0)
        restored = InferenceCore(engine.model, cfg=engine.config)
        assert restore_checkpoint(tmp_path, restored, device) == (1, 0)
        expected = processor.step(image, first_frame_pred=True)
        actual = restored.step(image, first_frame_pred=True)
        torch.testing.assert_close(actual, expected, rtol=0, atol=0)
