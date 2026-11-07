# Hermes 插件包

本目录包含 `comfy-workflows 0.6.5` 和 `ubuntu-runtime 0.3.1` 两个相邻安装的 Hermes 插件。生图、编辑、任务和图片索引在 Hermes 侧；工作流编译、ComfyUI API、服务管理和防睡眠锁在独立的 [Ubuntu 包](../ubuntu/README.md)。跨机调用只通过已有 SSH 私钥，不需要新的 HTTP 服务。

## 环境与兼容

- Python 3.12、uv、OpenSSH；Python 运行时代码使用标准库。插件使用 Hermes 已有的 Telegram 适配器依赖，不额外安装 PyTorch、ROCm 或 ComfyUI。
- Hermes 必须提供插件工具、原生命令、`register_platform_handler`、`pre_llm_call` 和可信会话路由接口。图片主动推送和按钮目前支持 Telegram；CLI 可查询本地任务，不主动推送。
- 桌面回图消费者通过 `register_gateway_ready` 在网关循环就绪后启动；插件注册期间只登记回调，不导入或等待网关。冷启动、热重载和后台任务恢复不需要模型发起新一轮对话，卸载插件会取消未启动回调与已运行消费者。
- 常驻工具 `tools.tool_search.eager` 和工具历史裁剪 `agent.tool_history_echo` 需要支持这些选项的 Hermes 版本。本仓库不修改 Hermes 核心；缺少接口时不能仅靠复制插件得到完整功能，见[兼容说明](../docs/hermes-compatibility.md)。
- 网络唤醒发送端当前支持同一局域网的 macOS，运行时查询实际 `enN` 路由和 IP；不经 VPN 广播，不提供 Linux/Windows WoL 发送适配。Ubuntu 必须已配置网卡 WoL。
- Ubuntu SDK 为 `0.7.2`，工作流协议为 `7`。版本和代码哈希由 `ubuntu/config/client-contract.json` 显式绑定。

## 安装

先按 [Ubuntu README](../ubuntu/README.md) 部署服务端。两端可以位于不同目录，`remote_root` 指向 Ubuntu 上 SDK 项目的绝对路径。

在本目录创建独立环境并运行离线测试；测试不会连接或唤醒 Ubuntu，也不会提交真实生图：

```sh
cd hermes
uv sync --locked
uv run python scripts/check.py
```

选定目标 Hermes profile，完成正在执行的任务后，显式安装两个插件：

```sh
uv run python scripts/install.py --hermes-home "$HOME/.hermes"
# 更新已有目录时显式使用 --replace；旧代码保留在目标 profile 的 plugin-backups/。
uv run python scripts/install.py --hermes-home "$HOME/.hermes" --replace
```

安装器只复制 `plugins/` 中的两个目录，不修改配置、密钥、任务数据或 Hermes 核心，不重启网关。两个插件必须处于同一 profile 的 `plugins/` 目录，有限后台 worker 会从相邻目录导入运行时客户端。

将 [config/config.example.yaml](config/config.example.yaml) 合并到该 profile 的 `config.yaml`。正式配置路径为 `plugins.entries.<plugin_id>.settings`；两个插件填写相同的以下值：

| 配置 | 说明 |
|---|---|
| `ssh_host` | 必填，可信的 `user@IPv4`，不接受模型覆盖 |
| `remote_root` | 必填，Ubuntu SDK 绝对目录；不会在远端展开 `~` |
| `identity_file` | 本机私钥路径，默认 `~/.ssh/id_ed25519`；只传给 ssh，不读取内容 |
| `mac_address` | 可选，Ubuntu 有线网卡 WoL MAC；与 `lan_cidr` 成对配置 |
| `lan_cidr` | 可选，WoL 直连局域网 CIDR；两项都留空时仅管理已在线的主机 |

示例地址属于文档保留网段，必须替换。先在本机手动完成 SSH 主机指纹验证；插件使用 `BatchMode=yes` 和 `StrictHostKeyChecking=yes`，不会接受新指纹或询问密码。

启用所选 profile 的插件，再按现有方式重启 Hermes 网关并使用新会话：

```sh
hermes plugins enable ubuntu-runtime
hermes plugins enable comfy-workflows
```

修改插件代码后，重新生成客户端契约，并将该文件同步到 Ubuntu 项目的同名位置；只同步文件，不复制客户端源码到 Ubuntu：

```sh
uv run python scripts/export_contract.py --output ../ubuntu/config/client-contract.json
```

哈希覆盖插件根目录的 Python 文件与 `plugin.yaml`，与进程加载时冻结的哈希一致。新提交或服务变更遇到 `stale_copy`、缺失契约或不匹配时会拒绝执行；先同步代码与契约，再重启网关。历史查询仍可使用。

## 模型调用

正常只需 `comfy_image`：自动准备 Ubuntu、确认 ComfyUI 真正可生图、提交并在后台下载。它快速返回本地任务 ID，进度、结果和处理按钮由程序直接送到原聊天，不需要模型轮询。

```json
{"prompt":"白色几何机器人站在彩色积木旁，清晰平面插画","title":"积木机器人","preset":"fast","size":"512x512"}
```

```json
{"prompt":"以 <image1> 为底图，保持构图与物体形状；只采用 <image2> 的橙蓝配色，不加入第二张图中的其他物体。","title":"机器人配色编辑","images":["img_000001","/absolute/reference.png"],"preset":"quality","size":"auto"}
```

| 参数 | 约束与默认值 |
|---|---|
| `prompt` | 必填，1–8000 字符，超限报错，不截断 |
| `images` | 有序 0–10 张本机 PNG 路径或 `img_000001`；无图为文生图 |
| `preset` | 默认 `fast`：LoRA、6 步、Euler/Pruna 自定义 sigmas；`quality`：无 LoRA、25 步、Euler/simple，文生图和编辑均沿用保存的 ComfyUI 官方模板 |
| `size` | 默认 `auto`：文生图 1024×1024，编辑 latent 跟随第一张参考图处理后的尺寸；可明确指定比例或 `宽x高` |
| `title` | 可选，最多 80 字符，描述生成意图，供后续查找；不是视觉核验结果 |
| `seed` | 文生图默认固定 `0`；edit 默认随机并避开已知参考图生成 seed，显式冲突自动换值；无符号 64 位，`seed + count - 1 < 2^64` |
| `count` | 默认 1，范围 1–4，逐张 seed 递增 |

指定宽高为 64–2048、32 的倍数；比例支持 `1:1`、`2:3`、`3:2`、`3:4`、`4:3`、`9:16`、`16:9`、`21:9`，约 1MP、32 对齐。精确 9:16 可用 `576x1024` 或 `864x1536`。编辑通常保留 `size=auto`：客户端传递 `follow_image`，服务端按参考图预算（默认约 1024×1024 像素面积）等比缩放并对齐后，由编码器输出采样 latent；不会预先锁定原图宽高。输入每张最多 20 MiB，总计最多 100 MiB。

CFG 固定 1、负向提示词为空，不暴露任意 steps、CFG 或 LoRA 参数；其他组合在用户测试中出现不稳定结果。单图编辑使用自然语言；多图按数组顺序使用 `<image1>`、`<image2>`，没有下划线。底图放第一张，并逐图说明参考用途。

Qwen Image 2.1 的重要约束是避免用参考图的生成 seed 再次编辑该图。客户端在上传或准备服务前，对实际输入副本计算文件 SHA256，查询 profile 内 `jobs.sqlite3` 的来源索引。字节相同的副本可通过哈希匹配，重新保存后若哈希未登记则标记为未知来源。所有参考图的已知直接生成 seed 均纳入校验，`count` 的整个连续 seed 区间须避开这些 seed。

edit 未提供 seed 时选择随机新值；显式 seed 命中冲突时也自动替换。返回的 `seed_used` 是实际使用值，`seed_policy` 说明请求值、实际值、选择原因与未知来源数量。未知来源图可继续使用；未指定 seed 时随机，显式指定且未发现冲突时保留，不能把来源未知理解成已证明安全。已选 seed 在远端提交前持久化，任务恢复与重试保持可复现；已尝试提交的任务不会因校验重新生成。

`comfy_task` 用于按需查询、取消或列出当前聊天的图片标题；旧 `comfy_generate`、`comfy_edit`、`comfy_tasks`、`comfy_workflows` 保留兼容入口。后两者不会自动完成新入口的整条后台编排。详细接口见[生图插件说明](plugins/comfy-workflows/README.md)。

## 直接命令与生命周期

Telegram 命令、按钮与已连接桌面会话中的以下命令直接由程序处理，不调用 LLM：

```text
/comfy run {"prompt":"几何机器人","title":"机器人","size":"512x512"}
/comfy status <task_id>
/comfy cancel <task_id>
/comfy resume <task_id>
/comfy retry <task_id>
/comfy resend <image_id>
/comfy images 机器人
```

`resume` 只恢复对已知任务的观察，`resend` 只补发已有图片。提交响应丢失会保留 `submission_uncertain`，不会自动重提。取消是合作式请求，不能视为死 GPU 已恢复；`stalled` 是停滞线索，也不是确认 GPU 故障。

独立管理工具 `ubuntu_status` 只读、不唤醒；`comfy_service` 的 `ensure_ready` 才允许 WoL，其他操作不会唤醒离线主机。`stop`、`restart` 保护活动队列；`recover` 针对已诊断异常的受管实例执行有限恢复。退出信号超时按既有策略自动升级，不要求手工确认命令；所有信号都核对 PID、进程启动标识和归属。详见[运行时说明](plugins/ubuntu-runtime/README.md)。

```sh
hermes ubuntu-runtime ubuntu_status --args '{}'
hermes ubuntu-runtime comfy_service --args '{"action":"ensure_ready"}'
hermes ubuntu-runtime comfy_service --args '{"action":"status","operation_id":"UUID"}'
```

Ubuntu 启停始终由服务端管理脚本调用配置的原 `run_comfyui.sh`，保留其 HSA/MIOpen/uv 参数和保护依赖检查。HTTP 活着不代表 GPU 健康，服务就绪需要实际烟测。插件不修改电源设置，不自动卸载模型，不安装登录自启或系统常驻服务。

## 数据和维护

任务数据放在所选 profile 的 `plugin-data/comfy-workflows/`，运行时操作收据放在 `plugin-data/ubuntu-runtime/`；不写入本仓库。收据包含请求、实际参数、seed、版本和哈希。产物遵循服务端数量上限，本机下载副本同步限额；轻量标题索引保留，图片删除后标记 `expired`。图片 hash/seed 来源记录独立于图片及大回执保留，清理后仍可识别已知冲突；登记来源时不保存提示词或聊天内容。

后台观察 worker 独立于网关、SSH 和终端，标准输入关闭，日志写入任务目录，并忽略 SIGHUP。需要手工管理某个观察进程时，使用记录中的 Python 和数据目录：

```sh
COMFY_WORKER_PYTHON=/path/to/hermes/python \
  /path/to/plugins/comfy-workflows/scripts/comfy-worker.sh status TASK_UUID DATA_DIR
# 支持 start、stop、restart、status；stop 只停止观察，不取消远端生成。
```

正常完成后直接发送图片和标题；下次聊天通过 hook 补入最多 4 张、1600 字符的轻量图片卡。图片卡不替代看图核验，也不把完整提示词、日志和执行图堆入模型上下文。

更新前完成活动任务；保留 profile 数据，安装新插件、同步契约、重启网关即可。回退时同时恢复两个插件、匹配的 Ubuntu SDK 和契约。任何部署步骤都不需要修改 ComfyUI 的 uv 锁文件或 ROCm 依赖。
