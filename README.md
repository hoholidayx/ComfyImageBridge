# ComfyImageBridge

在 ComfyUI 所在的 Ubuntu 主机提供内网 MCP 生图服务。自研框架通过标准 MCP 客户端提交文生图或多图编辑任务，在后台取回 PNG，并将附件写入原会话。

| 部署包 | 内容 |
| --- | --- |
| [ubuntu/](ubuntu/README.md) | MCP 适配、生图任务、工作流 SDK / CLI、快照和独立运维 |
| [hermes/](hermes/README.md) | 冻结的 Hermes 插件与固定 SSH 客户端，供回退使用 |

Ubuntu 使用自己的 Python 3.12 环境和 `uv.lock`。MCP 是可选依赖，工作流 SDK / CLI 仍可单独运行。ComfyUI、模型和受保护的 GPU 依赖由原有环境和 `run_comfyui.sh` 管理。

## 接入

自研框架的后端连接 `http://<Ubuntu 内网地址>:<端口>/mcp`。调用 `image_submit` 得到任务 ID，后台调用 `image_status` 与 `image_result`。每个任务返回一张 PNG。文生图和编辑使用相同入口，编辑支持最多 10 张有序参考图。

框架保存完整附件和任务记录，按需向模型提供简短回执或附件引用。MCP 只检查 ComfyUI 的渲染就绪状态，启停、恢复和唤醒由运维流程负责。

- [MCP 部署、工具契约与自研框架接入](docs/mcp.md)
- [Ubuntu 上线与实机验收待办](docs/mcp-rollout.md)
- [执行流程与数据边界](docs/architecture.md)
- [配置与部署边界](docs/configuration.md)
- [本地验证和发布](docs/development.md)
- [Ubuntu 硬件监控与睡眠恢复](docs/hardware-monitor.md)
- [故障排查](docs/troubleshooting.md)
- [来源与许可说明](NOTICE.md)

当前 Ubuntu 包版本为 **0.8.0**。Hermes 回退版本为 **0.7.2**，旧 SSH 客户端契约为 **7**。
