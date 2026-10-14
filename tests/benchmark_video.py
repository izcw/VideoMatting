"""独立完整视频验收；不会覆盖已有任务或原片。"""
import argparse
import json
import shutil
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.pipeline import ROOT, MattingEngine


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('--baseline', type=Path)
    args = parser.parse_args()
    folder = ROOT / 'data' / 'benchmarks' / time.strftime('%Y%m%d-%H%M%S')
    folder.mkdir(parents=True, exist_ok=False)
    shutil.copy2(args.source, folder / 'source')
    started = time.perf_counter()
    last_stage = None
    def report(**changes):
        nonlocal last_stage
        stage = changes.get('stage')
        if stage or 'completed_frames' in changes:
            print(json.dumps(dict(seconds=round(time.perf_counter()-started, 2), **changes), ensure_ascii=True), flush=True)
        last_stage = stage or last_stage
    metadata = MattingEngine().process(folder, report, threading.Event())
    result = dict(folder=str(folder), seconds=round(time.perf_counter()-started, 2), metadata=metadata)
    if args.baseline:
        import numpy as np
        from PIL import Image
        maximum, changed, total = 0, 0, 0
        for path in sorted((folder / 'alpha').glob('*.png')):
            before = np.asarray(Image.open(args.baseline / 'alpha' / path.name)).astype(np.int16)
            after = np.asarray(Image.open(path)).astype(np.int16)
            difference = np.abs(after-before)
            maximum = max(maximum, int(difference.max()))
            changed += int(np.count_nonzero(difference))
            total += difference.size
        result.update(alpha_max_difference=maximum, alpha_changed_pixels=changed, alpha_total_pixels=total)
    (folder / 'benchmark.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
