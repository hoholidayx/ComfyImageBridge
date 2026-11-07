# 配置与部署边界

## Ubuntu

保留完整 `ubuntu/` 目录结构，包括配置、脚本、工作流和示例；推荐部署为 `~/workspace/comfy-workflow-api`。本项目当前面向源码 checkout + `uv sync --locked` 的运行方式；不要只复制 wheel 后丢弃配套文件。

主要配置在 `config/settings.toml`：

| 配置 | 默认值 / 要求 |
| --- | --- |
| `base_url` | `http://127.0.0.1:8188`；与现有 ComfyUI 地址一致 |
| `data_dir` | 相对包根目录的 `data` |
| `service.comfy_dir` | `~/workspace/ComfyUI`；在 Ubuntu 用户环境中展开 |
| `service.run_script` | 相对 ComfyUI 根目录的 `run_comfyui.sh`，也可用绝对路径 |
| `service.uv_bin` | 空值时按环境变量、PATH 与用户 uv 路径查找 |
| `retention.max_tasks` | 100；清理模块管理的旧任务及其专用产物 |
| `retention.comfy_input_dir` / `comfy_output_dir` | 默认从 ComfyUI 根目录推导 `input/comfy-workflow-api`、`output/comfy-workflow-api` |
| `sleep_guard` | 默认开启，空闲宽限期 10 秒；需要可用 GNOME 用户会话 |
| `mcp.host` / `mcp.port` | 默认 `127.0.0.1:8787`。跨机访问时显式填写 Ubuntu 的可信内网 IPv4，见 [MCP 部署](mcp.md) |

配置文件本身不含密钥。实际主机的配置应单独备份，升级时人工合并新增键，避免覆盖。`COMFY_WORKFLOW_HOME` 可指定模块根目录；脚本与 SDK 必须指向同一根目录，以免使用不同数据和状态文件。

ComfyUI 的 uv 环境与本模块 `.venv` 相互独立。管理脚本会检查原启动脚本及受保护环境；原脚本需要改变依赖时应停止并诊断，不能改用裸 `python main.py` 绕过。启动参数、HSA/MIOpen 设置继续属于原脚本。

## Hermes 回退配置

模板位于 [`hermes/config/config.example.yaml`](../hermes/config/config.example.yaml)。配置合并到实际使用的 profile，两个插件填写相同的 SSH、远端目录与 LAN 参数：

- `ssh_host`：可信的 `user@IPv4` 目标；当前客户端会严格校验，不接受 SSH 别名或 DNS 主机名。
- `identity_file`：Hermes 主机上的私钥路径；默认 `~/.ssh/id_ed25519`，不复制到 Ubuntu 或仓库。
- `remote_root`：Ubuntu 包的**绝对路径**；不能使用 `~`，也不是 ComfyUI 目录。
- `mac_address`、`lan_cidr`：需要 WoL 时填写；不使用文档示例地址执行唤醒。

SSH 使用现有私钥和 known_hosts；先手工建立可信连接。网络唤醒需要有线网卡/固件/操作系统都已配置，并由同一可用 LAN 的控制端发送。当前自动检测发送接口使用 macOS 系统工具，VPN、跨网段或 Linux 控制端不能直接套用。

生图 worker 会调用相邻 `ubuntu-runtime` 插件，必须成对安装并启用。安装脚本只复制和备份，不编辑 profile、不修改 Hermes 核心、不自动重启网关。

## Hermes 核心兼容性

当前接口针对已有 Hermes fork 的插件上下文、绑定来源、直接平台发送与带范围的命令回调。已知源版本是 `hoholidayx/hermes-agent` 的提交 `2de4c987400d7753be523b4657449c4707b9df09`。

以下核心功能不由插件提供：

- `tools.tool_search.eager: [comfy_image]` 的选择性常驻；
- `agent.tool_history_echo: false` 的工具历史投影；
- 会话绑定和可信传输引用、直接投递、带 scope 的命令/回调接口。

没有这些 API 时，应先核对兼容性和诊断结果，不能承诺任意上游版本即装即用；不要为修兼容性关闭会话隔离或把凭据写入工具参数。本仓库不包含 Hermes 核心补丁。

## 旧 SSH 版本契约

`ubuntu/config/client-contract.json` 记录旧 SSH 桥接受的客户端版本与 hash。它由两个插件根目录的 Python 文件及 `plugin.yaml` 生成。MCP 使用独立的工具契约。

发布运行代码变更后重新导出，客户端与 Ubuntu 包一起更新。服务端不再依赖客户端源码副本来检查版本。缺失或无效的 manifest 会报告错误并阻止新的生成提交；现有任务的查询/诊断按接口自身规则保留。
