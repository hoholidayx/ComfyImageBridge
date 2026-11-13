# Ubuntu MCP 上线与实机验收

## 2026-11-13 独立客户端复验

从 Mac 通过 Streamable HTTP 直连 Ubuntu 上的 Comfy MCP 0.8.0，使用合成几何素材。Ubuntu 部署文件与仓库版本核对一致，实际配置单独保留。ComfyUI 0.44.0 使用 AMD Radeon RX 7900 XTX 和 ROCm。手动启动的终端实例退出后，项目管理器通过原启动脚本启动 ComfyUI，渲染就绪烟测通过。独立 SSH 连接确认 MCP 和 ComfyUI 后台 PID、监听归属及日志，生成结束后队列为空。

| 验收项 | 结果 |
| --- | --- |
| MCP 可用性 | 内网可访问 `/healthz`，发现提交、查状态和取图三个工具。错误 Host 返回 421，错误 Origin 返回 403。未受管的 ComfyUI 实例触发 `backend_unavailable`，未创建任务。 |
| ComfyUI 文生图 | `fast`，512×512 PNG，客户端从提交到取图约 3.4 秒，默认 seed 为 0。 |
| ComfyUI 双参考编辑 | `fast`，512×512 PNG，约 16.7 秒。保留立方体和球的构图并应用第二张参考图的背景配色，冲突 seed 自动替换。 |
| 重放和取图 | 关闭第一条客户端连接后，用同一 ID 重放并续查。两个任务各对应一次 ComfyUI 执行。参数变更触发冲突，重复下载的 SHA-256 一致，客户端每个任务只保存一份图片和一份回执。 |
| OpenAI Images 兼容供应商 | 复用原有 Sunburst 连接和凭据，通过本机独立 MCP 适配器实际调用供应商的 `/images/generations`。模型 `gpt-image-2.6-sunburst` 返回 1536×1024 PNG，约 26.2 秒。重连和重复取图通过。 |

Ubuntu 的 Comfy MCP 提供 `/mcp`。在该服务上探测 `/v1/images/generations` 和 `/v1/images/edits` 均返回 404。OpenAI 兼容实测覆盖独立适配器的供应商文生图路径。该适配器的临时测试实例在验收结束后已关闭。

后续使用[独立 MCP 验收入口](mcp.md#独立验收)，项目停止 Aion 专用适配。若日后接入其他宿主框架，原会话投递由该宿主独立验收。

## 2026-11-08 首次部署

2026-11-08 已部署 Ubuntu 0.8.0，MCP 和 ComfyUI 均通过最终状态检查。服务端验收使用独立的内网 MCP 客户端和合成几何素材。

| 验收项 | 结果 |
| --- | --- |
| 部署与回退 | 已备份原部署源码与配置，合并 MCP 监听配置并按锁文件安装。旧 Hermes SSH 契约仍可用于回退。 |
| 环境与启动 | 已核对原启动脚本及 HSA、MIOpen、uv 配置。通过原脚本完成 ComfyUI 渲染就绪烟测。启动脚本、ComfyUI 的 `pyproject.toml` 和 `uv.lock` 校验值一致，硬件监控进程持续运行。 |
| 独立进程 | 通过独立 SSH 会话核验 PID、启动标识、监听归属、日志、stdin 和 `/healthz`。实测 SIGHUP 后服务继续运行，外来监听占端口时拒绝启动。MCP 停止、启动和重启期间 ComfyUI 实例保持就绪。 |
| 内网协议 | 发现三个生图工具，提交工具为单图接口。错误 Host 返回 421，错误 Origin 返回 403。ComfyUI 停止时提交返回 `backend_unavailable`，请求未创建任务。 |
| GPU 文生图 | `fast` 生成一张 512×512 PNG，默认 seed 为 0。 |
| GPU 双参考编辑 | `fast` 按原图、色板的顺序输入，`auto` 生成一张 1024×1024 PNG。立方体保持原构图并应用参考色板，显式冲突 seed 自动替换。 |
| 重放与归档 | 两个请求各对应一条已确认的 ComfyUI 提交。同 ID 重放、客户端重连和 MCP 重启后均可取回同一 PNG，SHA-256 一致。示例客户端为每个任务保存一张附件和一条完成记录，模型投影仅含状态和附件引用。 |

Ubuntu Python 3.12.3 的暂存检查发现无效邮件抄送地址可触发标准库 `IndexError`。修正校验异常处理后，Ubuntu 的 261 项离线测试与快照检查通过，本地完整离线检查 404 项通过。

需要回退时，可停止 MCP，将客户端切回原入口，并从部署前备份恢复需要回退的 Ubuntu 文件。接口和管理命令见 [MCP 部署与接入](mcp.md)。
