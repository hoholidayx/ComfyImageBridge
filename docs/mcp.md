# MCP 生图部署与接入

## Ubuntu 部署

在 Ubuntu 上以管理现有 ComfyUI 的同一用户运行。保留完整 `ubuntu/` 部署目录、现有 `config/settings.toml` 和数据目录。MCP 和 ComfyUI 各自拥有管理命令。MCP 管理脚本需要 Linux pidfd 支持，以便按进程身份发送停止信号。

```bash
uv sync --locked --extra mcp
scripts/mcp-service.sh start
scripts/mcp-service.sh status
```

`scripts/mcp-service.sh` 也提供 `stop` 和 `restart`。运行记录和日志位于 `<data_dir>/mcp-service/`。启动 MCP 时检查自身的 PID、监听归属和 `/healthz`。停止 MCP 会等待正在处理的后端调用收尾。等待超过 120 秒时，脚本返回超时，可用 `status` 和日志继续检查。ComfyUI 就绪由 `scripts/comfyui-service.sh status` 单独检查。

```toml
[mcp]
host = "127.0.0.1"
port = 8787
max_pending_tasks = 8
```

默认只监听回环地址。跨机接入时，把 `host` 改为 Ubuntu 实际的私有 IPv4。客户端使用相同 IP 和端口连接 `http://<Ubuntu 内网地址>:8787/mcp`，传输选择 Streamable HTTP。服务使用明文 HTTP 和零凭据配置，并校验 Host 及浏览器请求的 Origin。该端口面向单一可信内网，能访问端口的设备都可以提交任务，持有任务 ID 即可查询和取图。

## 工具契约

| 工具 | 输入 | 输出 |
| --- | --- | --- |
| `image_submit` | `request_id`、`prompt`，可选 `preset`、`size`、`seed`、`images` | `task_id` 和 `state` |
| `image_status` | `task_id` | `task_id`、`state`，有错误时附 `error_code` |
| `image_result` | `task_id` | 一张 PNG 的 MCP `ImageContent`，结构化字段包括 `task_id`、`width`、`height`、`sha256` 和 `seed_used` |

- `request_id` 是框架在调用前生成并保存的标准小写 UUID，同时也是返回的 `task_id`。同一次提交重试时沿用原 ID 和原参数。相同请求返回原任务，同一 ID 配不同请求返回 `request_conflict`。
- `prompt` 最多 8000 字符。默认 `preset=fast`，使用 LoRA、6 步。指定 `quality` 使用无 LoRA、25 步。CFG 固定为 1。
- `size=auto` 时，文生图使用 1024×1024，编辑跟随第一张参考图经工作流处理后的尺寸。也支持 `宽x高`，每边为 64 至 2048 且为 32 的倍数，以及比例 `1:1`、`2:3`、`3:2`、`3:4`、`4:3`、`9:16`、`16:9`、`21:9`。
- 省略 `images` 或传空数组即文生图。编辑接收 1 至 10 张按顺序排列的 PNG base64 字符串。每张最多 20 MiB、每边最多 8192 像素、面积最多 3200 万像素。解码后合计最多 32 MiB，HTTP 请求体上限 48 MiB。参考图由框架发送原始字节。第一张是编辑底图，其余可在提示词中按 `<image2>` 等标记引用。
- `seed` 和 `seed_used` 使用无符号 64 位整数的十进制字符串。文生图默认 `0`。编辑默认随机，显式 seed 与任一参考图的已知生成 seed 冲突时会自动换值。保留原始 PNG 字节可以命中 SHA-256 来源索引。

任务状态为 `pending`、`submitting`、`queued`、`running`、`completed`、`failed`、`submission_uncertain` 或 `expired`。`completed` 时取图。提交应答丢失或已提交任务的后端状态无法查证时，进入 `submission_uncertain`，框架使用原任务 ID 续查。服务在 `wait_timeout` 窗口内自动查证，超时后保留待诊断状态并释放队列名额，框架可停止轮询。工具业务错误以 MCP 错误结果返回，调用方可区分 `invalid_request`、`request_conflict`、`backend_unavailable`、`queue_full`、`task_not_found`、`result_not_ready` 和 `result_expired`。已结束任务按 `retention.max_tasks` 保留，过期任务的请求 ID 仍用于防止重提。

ComfyUI 已完成但取图暂时失败时，服务在 `wait_timeout` 内重试取图。产物缺失或取图超时记录为 `output_unavailable`，校验失败记录为 `output_invalid`。

## 自研框架侧

1. 保存调用归属的逻辑会话 ID 和生成的 `request_id`，把用户选定的会话附件按顺序解析成 PNG 字节。模型只选择提示词、档位、尺寸及附件引用。
2. 用 MCP 客户端执行 `image_submit`。收到回执后保存 `task_id`，由框架后台用 `image_status` 观察状态；重连时恢复原 ID。提交响应丢失时沿用保存的请求 ID 和相同参数。
3. 调用 `image_result`，核对 SHA-256 并保存附件。以 `(原会话 ID, task_id)` 为唯一键写入一条完成记录。后续读取该记录时，仅把状态和可引用的附件 ID 投影给模型；需要看图或继续编辑时，再由框架加载 PNG。

[客户端示例](../ubuntu/examples/mcp_client.py) 使用合成图片模拟后台取图和重复写入。下列命令在 Ubuntu 包目录运行，默认使用离线替身。

```bash
uv run --locked --extra mcp python examples/mcp_client.py --state-dir "$(mktemp -d)"
```

实际客户端可用 `--url` 显式连接部署好的 MCP 入口。该命令会提交真实生图请求。可重复传入 `--image` 指定有序参考图，也可传入 `--task-id` 续查已有任务。

## 独立验收

[验收脚本](../ubuntu/examples/verify_image_mcp.py) 可从 Mac 或 Ubuntu 运行。先通过各自的管理脚本确认 Comfy MCP 健康、ComfyUI 就绪。在仓库根目录执行，将示例 IP 换成实际内网地址。

```bash
uv run --project ubuntu --locked --extra mcp python ubuntu/examples/verify_image_mcp.py \
  --url http://192.0.2.10:8787/mcp

uv run --project ubuntu --locked --extra mcp python ubuntu/examples/verify_image_mcp.py \
  --url http://192.0.2.10:8787/mcp --generate --edit --size 512x512
```

默认只发现工具并查询一个不存在的任务。加 `--generate` 后提交一次合成文生图，再用新连接重放同一 ID、检查冲突、轮询、重复取图并校验 PNG 与 SHA-256。`--edit` 追加一次使用生成图和仓库合成色板的参考图编辑。每个任务的本地图片和回执存于临时目录，脚本退出时自动清理。服务端任务仍按各自的保留规则管理。`--wait-seconds` 调整每个任务的轮询时间。脚本把原请求 ID 打印到 stderr，超时或连接中断时应按原 ID 续查。连接 Comfy MCP 时可用示例客户端的 `--task-id`。

同一脚本可验收通过 MCP 暴露的 OpenAI Images 兼容供应商。先启动已配置供应商 URL、模型和凭据的独立 MCP 适配器。将 MCP 访问令牌安全加载到环境变量，供应商 API 密钥由适配器读取。示例回环 URL 对应适配器的模型入口。

```bash
uv run --project ubuntu --locked --extra mcp python ubuntu/examples/verify_image_mcp.py \
  --url http://127.0.0.1:8790/mcp/example --token-env IMAGE_MCP_TOKEN --generate
```

省略尺寸时由服务端选择默认尺寸。经兼容供应商运行 `--generate` 可能产生实际费用。历次结果见[实机验收记录](mcp-rollout.md)。
