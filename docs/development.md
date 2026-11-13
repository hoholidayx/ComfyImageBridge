# 开发与升级

## 本地验证

在仓库根目录运行：

```bash
uv run --project ubuntu --locked --extra mcp python scripts/check.py
```

脚本以锁定环境运行 Ubuntu MCP、SDK、管理器及快照测试，并验证冻结的 Hermes 回退契约。测试使用本机替身或回环连接，不需要 GPU、SSH 或运行中的 ComfyUI。首次重建 uv 环境可能需要下载依赖。

单独验证两包：

```bash
uv run --project ubuntu --locked --extra mcp python ubuntu/scripts/check.py
uv run --project hermes python hermes/scripts/check.py
```

真实生成属于显式验收步骤，请使用几何示例，并确认现场 ComfyUI/GPU 正常。当前打包验证不会擅自执行真实生成、唤醒、关机或重启。

## 代码位置

| 修改目标 | 位置 |
| --- | --- |
| Hermes 工具定义、任务与投递 | `hermes/plugins/comfy-workflows/` |
| 客户端 SSH / LAN / WoL | `hermes/plugins/ubuntu-runtime/` |
| SDK 编译、HTTP、存储与清理 | `ubuntu/src/comfy_workflow_api/` |
| MCP 协议、生图任务、进程管理 | `ubuntu/src/comfy_workflow_api/mcp_server.py`、`image_jobs.py` 与 `ubuntu/scripts/mcp-service.sh` |
| SSH 服务端与管理 worker | 同目录下的 `workflow_bridge.py`、`runtime_bridge.py`、`runtime_operation_worker.py` |
| 受管 ComfyUI 服务 | `ubuntu/scripts/comfyui-service.py` |
| 当前快照与允许拓扑 | `ubuntu/workflows/` |

## 发布步骤

1. 保持 Python 3.12 和独立 uv 环境。SDK 基础安装使用标准库，MCP 依赖放在 `mcp` 可选组。
2. 更新 Ubuntu 包版本和必要文档，运行根检查脚本与 `git diff --check`。部署后的真实生成使用[独立 MCP 验收入口](mcp.md#独立验收)，结果记入[实机验收记录](mcp-rollout.md)。
3. 仅在维护冻结的 Hermes 客户端运行代码或 `plugin.yaml` 时，更新插件版本并导出旧客户端 manifest：

   ```bash
   uv run --project hermes python hermes/scripts/export_contract.py --output ubuntu/config/client-contract.json
   ```

4. 发布前核对内容只包含源码、文档和合成示例，排除用户实际配置、图片、任务、模型及密钥。

工作流修改通过显式同步产生新快照；同步前先从 ComfyUI 保存工作流，再在测试环境校验新拓扑与采样通路。分发前去掉用户默认提示词和参考图名，并更新内容 hash。更换拓扑不能只复制旧 allowlist。

## 数据迁移

本次整理不迁移旧图片或历史任务。若后续需要迁移，应单独备份数据与实际配置，保持任务 ID、服务端文件命名空间及权限一致；运行中的 worker 和防休眠 lease 不能当成可直接迁移的静态文件。不要把运行态数据作为 Git 版本升级的一部分。
