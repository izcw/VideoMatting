# VideoMatting 远程 API 与 CLI

适用于 VideoMatting 1.1.1，集成 API 版本仍为 v1，原有请求路径与任务格式保持兼容。`python cli.py --version` 可以查看客户端版本。

在显卡电脑运行「一键启动.cmd」，工作机通过启动窗口显示的地址连接，例如 `http://192.168.1.20:8765`。模型、任务、日志和算力都位于显卡电脑。CLI 只依赖 Python 3.10 以上的标准库，可单独将 `cli.py` 复制到工作机使用。

## 访问密钥

第一次启动会在显卡电脑的 `data/api-token.txt` 自动生成访问密钥。将这个文件安全复制到工作机，例如保存为 `gpu-api-token.txt`，不要提交到版本控制或放入网页目录。源码和完整打包版均支持。

也可在启动服务前设置 `VIDEOMATTING_API_TOKEN` 环境变量，优先于密钥文件。轮换文件密钥时先停止服务，删除 `data/api-token.txt` 后重新启动；同步更新工作机保存的副本。

所有 `/api/v1` 请求都需要请求头：

```text
Authorization: Bearer <访问密钥>
```

新增集成接口需要密钥；原网页与既有 `/api/jobs` 接口仍保留原来的局域网共享模式，并未增加账号登录。HTTP 仅用于可信局域网，不加密传输。系统后端或 CLI 可直接调用；跨域浏览器调用默认不开放。

## CLI 示例

以下命令在工作机执行，将地址替换成显卡电脑的实际 IP。无需在工作机安装 PyTorch、模型或显卡驱动。

```powershell
python cli.py --server http://192.168.1.20:8765 --token-file gpu-api-token.txt health
python cli.py --server http://192.168.1.20:8765 --token-file gpu-api-token.txt system
python cli.py --server http://192.168.1.20:8765 --token-file gpu-api-token.txt upload "D:\视频\原片.mp4" --wait --output "D:\视频\透明结果.webm"
```

源程序也可使用 `.venv\Scripts\python.exe cli.py ...`；完整打包版可用 `runtime\python.exe cli.py ...`。工作机只复制 `cli.py` 时用其已有的 Python。

可以设置 `VIDEOMATTING_SERVER` 与 `VIDEOMATTING_API_TOKEN` 环境变量，省略地址及密钥文件参数。不要把实际密钥写进脚本仓库。

任务命令，`任务ID` 使用上传接口返回的完整 UUID：

```powershell
python cli.py --server http://192.168.1.20:8765 --token-file gpu-api-token.txt list --all
python cli.py --server http://192.168.1.20:8765 --token-file gpu-api-token.txt status 任务ID
python cli.py --server http://192.168.1.20:8765 --token-file gpu-api-token.txt wait 任务ID --timeout 3600
python cli.py --server http://192.168.1.20:8765 --token-file gpu-api-token.txt cancel 任务ID
python cli.py --server http://192.168.1.20:8765 --token-file gpu-api-token.txt resume 任务ID
python cli.py --server http://192.168.1.20:8765 --token-file gpu-api-token.txt download 任务ID --output result.webm
python cli.py --server http://192.168.1.20:8765 --token-file gpu-api-token.txt download 任务ID --mode matte --output matte.mp4
python cli.py --server http://192.168.1.20:8765 --token-file gpu-api-token.txt download 任务ID --mode custom --color "#3b82f6" --audio --output background.mp4
python cli.py --server http://192.168.1.20:8765 --token-file gpu-api-token.txt delete 任务ID --yes
```

上传不加 `--wait` 时立即返回任务记录；加 `--wait --output` 则等待完成并下载透明 WebM。下载默认不覆盖文件，明确需要覆盖时可对 `download` 使用 `--overwrite`。删除会同时永久移除远端任务的原片副本、结果、遮罩和检查点。

下载结果返回 `file`、`bytes`、`sha256`。客户端检查响应长度，完整下载后才发布结果文件；断线、内容不足或保存失败时清理临时文件。1.1.1 不跟随 HTTP 重定向，请直接使用显卡电脑的实际服务地址。

进度变化写到标准错误，最终 JSON 写到标准输出；CLI 输出统一使用 UTF-8，便于系统解析中文日志和文件名。退出码 0 表示调用成功，1 表示连接/接口错误，等待任务以失败、取消或中断结束时为 2。等待超时或按 Ctrl+C 只停止客户端等待，远端任务仍继续；取消任务需单独调用 `cancel`。HTTP 请求默认最多等待 180 秒；任务等待使用独立轮询，`--timeout 0` 不限总等待时间。

## 远程诊断与日志

```powershell
python cli.py --server http://192.168.1.20:8765 --token-file gpu-api-token.txt diagnostics
python cli.py --server http://192.168.1.20:8765 --token-file gpu-api-token.txt diagnose
python cli.py --server http://192.168.1.20:8765 --token-file gpu-api-token.txt diagnose-status
python cli.py --server http://192.168.1.20:8765 --token-file gpu-api-token.txt logs --name system-check --lines 300
python cli.py --server http://192.168.1.20:8765 --token-file gpu-api-token.txt logs --name server-error
python cli.py --server http://192.168.1.20:8765 --token-file gpu-api-token.txt logs --job 任务ID --kind events
python cli.py --server http://192.168.1.20:8765 --token-file gpu-api-token.txt logs --job 任务ID --kind error
```

`diagnose` 启动固定的系统检测脚本并立即返回；用 `diagnose-status` 查询 `running/done/failed`，再读取 `system-check` 日志。一次只允许一个检测，不提供任意命令执行或任意本地文件读取。

`diagnostics` 汇总设备、资源占用、95% 阈值、近期任务和检测状态。任务进度事件记录在各任务的 `events.jsonl`，错误堆栈记录在 `error.log`；旧任务没有生成过的日志返回 `exists: false`。事件日志超过 2 MB 时轮换保留一份旧日志。日志接口返回末尾 1～2000 行，每次最多读取末尾 256 KiB；`truncated` 表示内容被截取。

## API 一览

所有路径以下述前缀开头：`http://显卡电脑IP:8765/api/v1`。

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/health` | 服务版本、设备、模型状态、局域网地址 |
| GET | `/system` | CPU、GPU、内存和显存监控 |
| GET | `/diagnostics` | 诊断汇总 |
| POST / GET | `/diagnostics/system-check` | 启动检测 / 查询检测状态 |
| GET | `/logs/{name}?lines=200` | `server`、`server-error` 或 `system-check` 日志 |
| POST | `/jobs` | multipart 上传，字段名 `file`，成功返回 202 |
| GET | `/jobs?include_hidden=false` | 任务列表；可包含被网页隐藏的历史任务 |
| GET | `/jobs/{id}` | 完整任务记录、阶段、进度、错误、用时 |
| POST | `/jobs/{id}/cancel` | 取消任务 |
| POST | `/jobs/{id}/resume` | 恢复失败、中断或取消的任务，返回 202 |
| DELETE | `/jobs/{id}` | 删除任务及本机处理文件；活动任务返回 409 |
| GET | `/jobs/{id}/logs?kind=events&lines=200` | 阶段事件或 `kind=error` 错误日志 |
| GET | `/jobs/{id}/preview-frame` | 当前透明预览缩略图，尚未生成时 404 |
| POST | `/jobs/{id}/export` | 导出选项，返回受保护的预览和下载相对路径 |
| GET | `/jobs/{id}/download` | 下载默认透明 WebM |
| GET | `/jobs/{id}/original`、`/result` | 原片预览或透明结果，支持 HTTP Range |

任务状态为 `uploading`、`queued`、`processing`、`done`、`failed`、`cancelled`、`interrupted`。轮询任务接口即可获取进度；同一任务重复恢复不会重复创建推理任务。队列上限为三个活动任务，满时 429；上传限制仍是 250 MB、60 秒、4K 像素量，质量策略与网页一致。

导出 JSON：

```json
{"mode":"transparent","color":"#3b82f6","audio":false}
```

`mode` 可选 `transparent`、`custom`、`matte`。返回的 `preview`、`download` 为以 `/api/v1/` 开头的相对路径，请继续携带访问密钥获取资源。`custom` 和 `matte` 下载 MP4，透明结果下载 WebM；`audio: true` 保留原片音频，不重新压缩已生成的视频流。

常见状态码：401 密钥无效，404 任务或素材不存在/尚未生成，409 状态冲突，413 文件过大，422 参数错误，429 队列已满。

自动交互文档：`http://显卡电脑IP:8765/docs`，点击 Authorize 输入密钥后测试。Swagger 页面资源需要网络；完整 OpenAPI JSON 始终由本机 `/openapi.json` 提供。

## Python 系统对接

将 `cli.py` 作为模块使用即可，无需额外安装 HTTP 库：

```python
from pathlib import Path
from cli import Client

client = Client("http://192.168.1.20:8765", Path("gpu-api-token.txt").read_text().strip())
job = client.upload("input.mp4")
result = client.wait(job["id"], timeout=3600)
if result["status"] == "done":
    client.download(job["id"], output="result.webm")
else:
    print(client.json(f"/api/v1/jobs/{job['id']}/logs?kind=error"))
```
