"""源码与绿色版共用的系统检测入口。"""
import platform
from pathlib import Path


def main():
    print('清影工坊 — 系统检测')
    print('系统：', platform.platform())
    print('处理器：', platform.processor() or '未识别')
    try:
        import psutil
        memory = psutil.virtual_memory()
        print('内存：总计', round(memory.total / 2**30, 1), 'GB；可用', round(memory.available / 2**30, 1), 'GB')
    except ImportError:
        print('内存监控依赖尚未安装。')
    try:
        import torch
    except ImportError:
        print('处理依赖尚未安装，请运行「一键启动.cmd」准备环境。')
        return 1
    print('PyTorch：', torch.__version__)
    print('内置 CUDA：', torch.version.cuda or '未包含（当前为 CPU 运行库）')
    if torch.cuda.is_available():
        try:
            print('显卡：', torch.cuda.get_device_name(0))
            print('显存：', round(torch.cuda.get_device_properties(0).total_memory / 2**30, 1), 'GB')
            with torch.inference_mode():
                value = torch.ones((256, 256), device='cuda')
                result = value @ value
                torch.cuda.synchronize()
                assert result[0, 0].item() == 256
            print('GPU 运算通过，程序将自动使用 CUDA GPU。')
        except Exception as error:
            print('GPU 运算失败，请核对 NVIDIA 驱动及显卡兼容性：', error)
            return 1
    else:
        print('当前使用 CPU 处理，建议使用具备 NVIDIA 显卡的设备。')
        if torch.version.cuda:
            print('如有 RTX 4060，请核对 NVIDIA 驱动。')
        else:
            print('GPU 加速需要 CUDA 版 PyTorch；二合一绿色版已内置。')
    root = Path(__file__).resolve().parent
    models = ('models/matanyone2.pth', 'models/sam2.1_hiera_tiny.pt',
              'models/torch/hub/checkpoints/deeplabv3_mobilenet_v3_large-fc3c493d.pth')
    missing = [name for name in models if not (root / name).is_file()]
    print('模型：', '齐备' if not missing else '缺失：' + '、'.join(missing))
    return 1 if missing else 0


if __name__ == '__main__':
    raise SystemExit(main())
