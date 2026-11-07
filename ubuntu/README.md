# Ubuntu：ComfyUI 工作流 SDK 与运行管理

这是运行在 ComfyUI 同一台 Ubuntu 主机上的独立包。提供标准库 HTTP 客户端、Python SDK、JSON CLI、工作流编译器、任务记录、数量清理、防睡眠和安全的服务管理。Hermes 客户端位于仓库的 [`../hermes/`](../hermes/)；远程执行逻辑由本包持有，客户端只通过 SSH 调用。

本包不安装 PyTorch、ROCm 或 ComfyUI，不复制模型、用户图片、历史任务、原机启动脚本，也不新增 HTTP 常驻服务。

## 安装与配置

需要已有 Python 3.12、uv，以及正常配置的 ComfyUI、模型和自定义节点。在 Ubuntu 上保留整个 `ubuntu/` 目录，例如复制到 `~/workspace/comfy-workflow-api`：

```bash
cd ~/workspace/comfy-workflow-api
uv sync --locked
uv run comfy-workflow --help
uv run python scripts/check.py
```

有缓存时可执行 `uv sync --locked --offline`。`dependencies = []` 表示运行时无第三方 Python 依赖；构建阶段使用 hatchling。安装在独立 `.venv`，与 ComfyUI 环境分开。

编辑 [`config/settings.toml`](config/settings.toml)：

| 项目 | 默认值与含义 |
| --- | --- |
| `base_url` | `http://127.0.0.1:8188`；管理功能面向本机 ComfyUI |
| `data_dir` | `data`；相对本包目录，也可用绝对路径 |
| `service.comfy_dir` | `~/workspace/ComfyUI`；展开当前用户主目录 |
| `service.run_script` | `run_comfyui.sh`；相对 ComfyUI 目录或绝对路径 |
| `service.uv_bin` | 空时使用 `COMFY_UV_BIN`、PATH 中的 uv，最后尝试 `~/.local/bin/uv` |
| `retention.max_tasks` | 保留最多 100 个符合清理条件的结束任务 |
| `retention.comfy_*_dir` | 留空时按 ComfyUI 根目录派生独立 input/output 子目录 |
| `sleep_guard` | 默认启用，空队列且无 lease 后等待 10 秒释放 |

`COMFY_WORKFLOW_HOME` 可指定本包运行根目录；SDK 的 `WorkflowAPI(root=...)` 和 CLI 的 `--root` 可以显式指定。`COMFYUI_URL` 或 `--base-url` 可覆盖 SDK HTTP 目标；跨主机目标不允许执行本地文件清理。

## 工作流与模型要求

两个公开别名共用一份已校验的编辑工作流结构：`qwen-image-2.1-t2i` 和 `qwen-image-2.1-edit`。文生图移除参考图入口，编辑保留按顺序连接的 1–10 张 PNG；共享模型加载与 LoRA 分支，尽量复用 ComfyUI 缓存。显存常驻仍受 ComfyUI 内存策略和 GPU 状态影响。

快照位于 [`workflows/manifest.json`](workflows/manifest.json) 引用的位置，只有当前版本，没有历史工作流。默认提示词和图片名已替换成几何机器人示例，拓扑未变。模型文件名仍保留原有配置：

- `qwen_image_2.1_int8_convrot.safetensors`
- `qwen3vl_8b_int8_convrot.safetensors`
- `qwen_image_2.1_vae_bf16.safetensors`
- `p_q_image_2.1_6_steps_v0.2_comfyui_bf16.safetensors`

ComfyUI 还需具备快照使用的 `TextEncodeQwenImage21`、`QwenImage21Cache`、`ResolutionSelector`、`ComfySwitchNode`、`SaveImageAdvanced` 和 fast 所需的 `ViggleTurboSigmas` 等节点。本仓库不会安装节点或下载模型。更换模型、LoRA 或节点拓扑后须更新适配器并验证，不能只替换文件名假定兼容。

编译和生成使用本地快照，不依赖 ComfyUI UI 中的默认提示词。若需显式从 UI 同步，先将快照导入 ComfyUI 并保存为 `qwen-image-2.1-unified.json`，再执行：

```bash
uv run comfy-workflow sync-workflows
```

拓扑不受支持时拒绝同步，保留现有快照。每个任务保存快照版本与 SHA256；不会自动跟随上游模板更新。

## 生图参数

| preset | 步数 / LoRA | 采样行为 |
| --- | --- | --- |
| `fast`（默认） | 6 步，启用 LoRA | Euler，Pruna v0.2 的自定义 sigma 序列与分辨率偏移 |
| `quality` | 25 步，不启用 LoRA | Euler / simple，沿用已选定的 ComfyUI 官方模板参数 |

文生图和编辑的 quality 当前均为 25 步。fast 不使用 quality 的 simple 调度；实际 `sampling_config` 会写入 receipt。CFG 固定为 1、负向提示词必须为空，其他组合因既有实测不稳定而拒绝。步数由 preset 固定，不能任意指定。

`prompt` 必填，最多 8000 字符，超限报错；直接使用本 SDK/CLI 时 `seed` 默认固定 0，不随机，范围为无符号 64 位整数。多图编辑按数组顺序连接 `<image1>`、`<image2>` 等入口。

**Qwen Image 2.1 编辑重要约束：生成图再次作为参考图 edit 时，应避免复用参考图生成时的 seed。** 当前模型配置的合成图交叉对照已复现同源 seed 的纹理劣化，不泛化为所有模型的通病。Hermes 客户端 edit 默认随机，并通过本机 hash/seed 索引自动替换已知冲突；SDK/CLI 与手动 ComfyUI 不使用该客户端索引，调用者应自行选择新 seed。文生图默认仍为0。

尺寸用 `output_size` 表达：

```json
{"mode":"explicit","width":512,"height":512}
```

```json
{"mode":"aspect_ratio","aspect_ratio":"9:16","megapixels":1,"multiple":32}
```

```json
{"mode":"follow_image"}
```

编辑默认使用 `follow_image`：采样 latent 取自 `TextEncodeQwenImage21` 的尺寸输出，随第一张参考图经过缩放和 32 对齐后的尺寸变化；fast 的 sigma 分辨率偏移使用同一 latent。`reference_resolution` 为 0–4096 且能被 32 整除，默认 1024，表示约 1024×1024 的像素面积并保留比例；0 表示按原始参考图尺寸做 32 对齐。跟随模式不会预先锁定原图像素宽高。

明确要求其他输出画布时可指定比例或宽高。支持比例为 `1:1`、`4:3`、`3:4`、`16:9`、`9:16`、`3:2`、`2:3`、`21:9`；指定宽高为 64–2048 且能被 32 整除，比例模式为 0.1–4 百万像素。例子均使用相对图片路径，CLI 相对于请求 JSON 所在目录解析。

## CLI 与 SDK

只编译，不向 ComfyUI 提交：

```bash
uv run comfy-workflow list-workflows
uv run comfy-workflow compile qwen-image-2.1-t2i --request examples/t2i.json
uv run comfy-workflow compile qwen-image-2.1-edit --request examples/edit-multi.json
```

真实生成与跨进程恢复：

```bash
uv run comfy-workflow health
uv run comfy-workflow submit qwen-image-2.1-t2i --request examples/t2i.json
uv run comfy-workflow status TASK_ID
uv run comfy-workflow wait TASK_ID --timeout 1200
uv run comfy-workflow fetch TASK_ID
uv run comfy-workflow cancel TASK_ID
uv run comfy-workflow run qwen-image-2.1-edit --request examples/edit-single.json --timeout 1200
```

所有命令输出 JSON。超时保留任务；提交响应不确定时记录原任务，不能重新提交来重试。取消只针对自身任务，运行中的取消是请求，需后续状态确认；不会清空全局队列。

```python
from comfy_workflow_api import WorkflowAPI
from comfy_workflow_api import batches

api = WorkflowAPI(root="/path/to/comfy-workflow-api")
task = api.submit("qwen-image-2.1-t2i", {
    "prompt": "A blue geometric robot beside colorful blocks, white background.",
    "preset": "fast", "seed": 0,
    "output_size": {"mode": "explicit", "width": 512, "height": 512},
})
api.wait(task["task_id"], timeout=1200)
result = api.fetch(task["task_id"])
# 可选：batches.submit(api, alias, request, count=2)，count 为 1..4，seed 递增。
```

SDK 提供 `health`、`list_workflows`、`compile`、`submit`、`status`、`wait`、`fetch`、`run`、`cancel` 和 `cleanup`。批次逐项排队，不增大 GPU latent batch。远程桥还执行客户端哈希校验；[`config/client-contract.json`](config/client-contract.json) 由仓库发布工具生成，缺失或不匹配时拒绝远程修改操作。

## 服务管理与恢复

首先检查原有 `run_comfyui.sh`，再调用本包管理器：

```bash
bash scripts/comfyui-service.sh status
bash scripts/comfyui-service.sh start
bash scripts/comfyui-service.sh ensure_ready
bash scripts/comfyui-service.sh stop
bash scripts/comfyui-service.sh restart
bash scripts/comfyui-service.sh recover
bash scripts/sleep-guard.sh status
```

`status` 只读，不唤醒、不生成、不获取锁。`start`/`ensure_ready` 可能启动原脚本并进行真实 64×64 fast 烟测；运行中已有正常任务时不会插入烟测。HTTP 健康、端口 LISTEN、进程归属与实际 GPU 出图分别检查。恢复纪元、代码和工作流变更会使旧 ready 失效。

后台运行通过独立会话、nohup、`/dev/null` 输入和日志重定向脱离 SSH；记录 PID、启动标识、命令与日志。停止前验证归属、启动标识和进程组，遇到外来监听进程拒绝误杀；端口检测只认 LISTEN。普通停止保护活动生成，SIGINT 最多等待 120 秒，再自动升级 TERM/KILL；显式 recover 在确认异常与归属后按恢复策略处理，不再要求手动确认命令。SIGKILL 后仍不退出时报告可能的驱动等待，不反复重启或擅自重启主机。

管理器会离线检查原 ComfyUI uv 锁及同步计划；若受保护依赖将变化则停止。不会编辑原启动脚本、ComfyUI 锁文件或电源设置。用户明确授权的可选系统监控、邮件与睡眠协调安装方式见 [硬件监控与睡眠恢复](../docs/hardware-monitor.md)。

## 产物、防睡眠与清理

```text
data/
├── inputs/<task_id>/
├── runs/<task_id>/       # 请求、快照、执行图、提交回执、历史、结果、日志、outputs/
├── batches/<task_id>/
├── comfyui-service/     # 服务状态、日志与烟测记录
├── runtime-operations/  # 有界远程管理操作的回执和 worker 日志
└── sleep-guard/         # supervisor、lease、运行观测
```

结果包含 prompt_id、实际参数、各张 seed、尺寸、SHA256、服务端位置、耗时及版本信息。状态包含 `observed_at`、运行耗时与保守的 `stalled` 判断，指标缺失不会伪装成 0% GPU。

ComfyUI 的输入和输出使用专属 `input/comfy-workflow-api/<task_id>/`、`output/comfy-workflow-api/<task_id>/`。清理只触及经任务记录确认的本模块文件，拒绝符号链接和非本机目标；保护活动任务、未下载的完成任务和当前任务。

```bash
uv run comfy-workflow cleanup --dry-run
uv run comfy-workflow cleanup
```

数量上限针对符合条件的结束生成任务，不是整个 data 目录的总文件上限；服务、批次与管理操作的审计记录不按此数量删除。生成流程会自动执行清理，不另外安装定时服务。

防睡眠 supervisor 使用真实 GNOME 锁，保护排队、生成与管理操作。lease 与持有进程身份绑定，结束后释放；队列空且无 lease 后等待宽限期释放锁。长龄 lease 只报警；网络异常或指标不可读时保守保留，不能按时间强行放睡。

## 验证范围

`uv run python scripts/check.py` 执行离线单元测试和快照完整性检查，不启动 ComfyUI、生成图片或触碰电源设置。

`uv run python scripts/verify_live.py` 会提交三个真实生成任务，并验证跨进程查询/等待/下载与输出尺寸；只能在部署完成、允许生成时手动运行。`scripts/verify_sleep_guard.py` 是 GNOME 集成验证，会短暂获取真实防睡眠锁，但使用隔离的模拟队列，不启动 ComfyUI。此次仓库整理只做离线验证，未重新验证当前 GPU 状态或部署到远端。
