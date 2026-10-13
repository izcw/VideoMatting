# VideoMatting 本地视频抠像工作台

“最近的处理任务”右上角可清空记录，确认后持久隐藏历史入口，不删除任何原片、结果、帧序列或任务文件；当前预览/下载仍保留。还有任务上传、排队或处理时禁止清空。此功能不释放磁盘空间。

4K 输入上限包含 3840×2160 和 4096×2160（以及等像素量的竖屏），最长 60 秒、最大 250 MB；上传阶段显示独立实时进度条、百分比与传输大小。尺寸校验通过并不保证 CPU/内存足以高效处理整段 4K 视频，当前开发机尚未验收完整 4K 抠像。

中文名称：清影工坊。拖动播放进度时同步显示两侧对应画面；拖动中暂时暂停，原本正在播放时松开后继续。阶段进度只在上传/排队/处理期间显示，逐帧阶段展示实时已处理/总帧数和百分比。Alpha Matte 模式隐藏背景选项。下载命名为 `VideoMatting-格式-12位任务ID.扩展名`，12 位取自内部 UUID 去除连字符后的前 12 位；内部任务与资源链接仍使用完整 UUID。

上传单人物原视频后，自动识别人物、精细视频抠像、导出透明 WebM。左边原视频、右边透明结果，同步播放/暂停/拖动进度，可切换棋盘格、黑底、白底。任务、结果和母版仅保存在本机，不上传云端，不替换 Client 素材。

## 使用

当前开发机已安装环境和模型。双击 `一键启动.cmd`，浏览器打开 `http://127.0.0.1:8765`；上传原视频后自动处理，完成后下载结果。停止服务可双击 `停止服务.cmd`，该入口只停止启动器登记的本工具进程。

保持启动窗口打开；关闭该窗口或按 Ctrl+C 即停止服务及子进程（正在处理的任务会中断）。再次启动若发现服务已运行，会复用现有服务，并提示关闭原启动窗口。启动提示使用中文，详细第三方错误日志位于 `data/server-error.log`。

左侧预览区域支持点击选择和拖放上传，上方“删除素材 / 重新制作”仅清空当前选择，保留磁盘历史。默认循环播放，空格同步暂停/继续；两侧可各自全屏，也可对比全屏。上传、解码、加载、预热、逐帧抠像、合成均显示阶段进度；逐帧开始后动态估算剩余时间，估算包含初始化开销，不能当作准确承诺。

默认透明 WebM：棋盘格、黑白底、绿幕和自定义颜色只改变预览，不改变透明下载。选择自定义背景模式：预览指定颜色，下载写入相同颜色的 H.264 MP4。选择 Alpha Matte：显示并下载黑白遮罩 H.264 MP4（白色保留、黑色移除、灰色半透明）。两种 MP4 均基于原帧/Alpha 母版生成，无需再次抠像；首次切换会等待格式准备。H.264 遇奇数宽高会补到偶数，最多增加一行或一列。播放循环不等于修复首尾动作接缝。

页面与本地处理无需按次付费，但不代表依赖模型具有免费商用许可，见文末许可说明。

新电脑先安装 Python 3.13、Node.js 与 pnpm 10.18，再运行 `安装环境.cmd`。首次需要联网下载依赖和模型，之后在权重齐备时可离线处理。默认安装为 CPU 版；NVIDIA GPU 机器需按 PyTorch 官方安装指南选择与驱动兼容的 CUDA 版 torch/torchvision，不直接照搬 CPU 锁文件。

## 实际处理流程

- 自动首帧选择：DeepLabV3 MobileNet V3 Large 定位人物，再使用 SAM 2.1 精细分割；长裙场景的提示框覆盖完整高度，避免仅选中头部或漏掉裙摆。根据模型置信度和人物区域覆盖率筛选结果，不要求用户提供 Matte。首帧提示模型有内部缩放，但逐帧 Alpha 推理保留原分辨率。
- 精细抠像：MatAnyone 2，十次首帧预热，逐帧时序记忆，不降采样处理。
- 高清合成：原片解码 PNG + 逐帧 Alpha PNG，FFmpeg `alphamerge`，VP9 Alpha / CRF 16 / 静音输出；不通过绿色预览片二次转码，不做锐化、超分或自动循环修复。
- 左侧为了兼容浏览器，会生成 CRF 16 的 H.264 预览。最终结果用的是原片 PNG，不使用左侧预览 MP4。
- 支持 MP4、MOV、WebM、MKV，最多 250 MB、60 秒、4K 像素量；当前以单人物、固定镜头为目标。多人物、复杂道具、透明面料可能误选，失败时明确报错，不退回伪抠像算法。
- 帧序列按检测到的平均帧率处理，适合固定帧率 AI 生成视频；可变帧率输入会按该帧率规范化，不保证保留原始 VFR 时间戳。

## 文件

`server/` FastAPI 后端；`web/` Vue 3 + TypeScript；`vendor/MatAnyone2/`、`vendor/SAM2/` 官方源码；`models/` 模型缓存；`data/jobs/<任务 UUID>/` 原片、原片预览、PNG 原帧和遮罩、结果与任务记录。数据不会自动删除，长视频的 PNG 母版会占用较多磁盘；确认不再需要后可人工删除对应任务目录。服务重启后，中断任务标为失败，可重新上传。

`models-manifest.json` 记录固定源码版本、官方模型来源和 SHA256；下载模型使用官方 GitHub assets API，安装器校验权重。源码、模型、venv、输出和构建目录不进入源码版本控制。

## 开发与测试

```powershell
.venv/Scripts/python.exe -m pytest tests -q
pnpm --dir web build
.venv/Scripts/python.exe -m uvicorn server.app:app --host 127.0.0.1 --port 8765
```

服务只绑定回环地址，拒绝跨站写请求，不提供任意本地路径访问或命令执行接口；媒体由任务 ID 和固定资源名称访问。

CPU 可以运行但较慢；高清素材的真实处理时间以任务记录为准。模型不会恢复原视频本来没有的面部细节，须在黑、白、棋盘格背景上人工检查发丝、衣袖、裙摆和边缘闪烁。

## 本机验证记录

- 2026-10-13 更新：新增 Windows 启动窗口退出测试、三模式导出接口与真实像素检查；单项窗口生命周期实测通过。Electron 验证了上传区域点击/拖放、分辨率、空格、跨周期循环同步、左右/对比全屏、三模式预览下载及清空素材。新增导出测试使用 64×64 母版验证合成背景色和黑白 Alpha，并对现有李清照完整样片进行了真实导出与播放。

- Windows / Intel CPU / PyTorch CPU：17 项接口与媒体测试通过；Vue TypeScript 检查、生产构建、Python 编译检查和依赖一致性检查通过。
- 已通过实际 `一键启动.cmd` 路径启动并重启后台服务，不再依赖 PowerShell 启动文件；启动失败会保留窗口提示与日志。
- 完整李清照原片实测：832×1108、30 fps、106 帧（约 3.53 秒），全自动处理用时 439.8 秒，输出 1,445,985 字节。
- 原片与结果同步播放/暂停/拖动、三种预览背景、下载链接通过 Electron 浏览器自动化检查。第 2 秒背景 Alpha 为 0，面部、衣袖和裙摆抽样 Alpha 为 255；人工检查左右画面未见前期的大面积漏抠。
- 早期调试样本已标记废弃，原文件仍保留；最终示例为最近任务“李清照-原片验证.mp4”。此记录只覆盖该样本，NVIDIA GPU、多人、复杂道具及 4K 长视频没有实际验收。

## 来源与许可

网页 Logo 使用内置 imagegen 工具生成，资产为 `web/public/logo-v1.png`，保留透明背景并用于页眉/网站图标。最终提示词：Use case: logo-brand. Create one polished minimal logo mark for VideoMatting, a local video background-removal workspace. A violet rounded video frame with a clean foreground silhouette lifting free from the frame, suggesting alpha extraction and precision. Flat vector-like geometric shapes, tasteful violet #625ad6 and dark graphite, elegant clean edges, legible at 40px. Square composition, symbol fills most canvas with modest padding. True transparent background. No text, no letters, no watermark, no mockup, no surrounding scenery.

MatAnyone 2：https://github.com/pq-yang/MatAnyone2（S-Lab 非商业许可，许可文件保存在 vendor 源码）；PyTorch/torchvision：https://github.com/pytorch/vision；FFmpeg：https://ffmpeg.org。下载、测试不等于商用授权确认，发布前需核对源码、权重和 FFmpeg 构建许可。
