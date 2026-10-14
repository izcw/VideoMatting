"""绿色版现场验收：实际运行 CUDA 运算，不只查看显卡名字。"""
import torch

print('清影工坊运行设备检查')
print('PyTorch：', torch.__version__)
print('内置 CUDA：', torch.version.cuda or '未包含')
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
        print('提示：检测通过不代表任意 4K 视频均能放入显存。')
    except Exception as error:
        print('GPU 运算失败，请核对 NVIDIA 驱动及显卡兼容性：', error)
else:
    print('未检测到可用 CUDA GPU，程序将使用 CPU。')
    print('如电脑有 RTX 4060，请先安装/更新 NVIDIA 官方驱动后重试。')
