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
| `retention.auto_cleanup` | 默认开启，MCP 工作期间每 5 分钟执行一次维护，启动时立即执行 |
| `retention.log_max_bytes` / `log_backups` | 单份 8 MiB，每个日志保留 3 份归档 |
| `retention.max_tasks` | 100；清理模块管理的旧任务及其专用产物 |
| `retention.comfy_input_dir` / `comfy_output_dir` | 默认从 ComfyUI 根目录推导 `input/comfy-workflow-api`、`output/comfy-workflow-api` |
| `sleep_guard` | 默认开启，空闲宽限期 10 秒；需要可用 GNOME 用户会话 |
| `mcp.host` / `mcp.port` | 默认 `127.0.0.1:8787`。跨机访问时显式填写 Ubuntu 的可信内网 IPv4，见 [MCP 部署](mcp.md) |

配置文件本身不含密钥。实际主机的配置应单独备份，升级时人工合并新增键，避免覆盖。`COMFY_WORKFLOW_HOME` 可指定模块根目录；脚本与 SDK 必须指向同一根目录，以免使用不同数据和状态文件。

ComfyUI 的 uv 环境与本模块 `.venv` 相互独立。管理脚本会检查原启动脚本及受保护环境；原脚本需要改变依赖时应停止并诊断，不能改用裸 `python main.py` 绕过。启动参数、HSA/MIOpen 设置继续属于原脚本。

从 0.8.x 升级时，按新版文件清单更新部署目录。移除旧版的 `config/client-contract.json` 及 Python 包中的 `workflow_bridge.py`、`runtime_bridge.py`、`runtime_operation_worker.py`，保留实际配置和运行数据。外部调用方使用 [MCP 接口](mcp.md)。
