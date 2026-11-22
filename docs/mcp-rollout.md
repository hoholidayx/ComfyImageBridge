# Ubuntu MCP 上线与实机验收

## 0.9.3 · 局域网直连待部署

仓库和 Mac 隔离实例已验证私网 IPv4 监听、Host 校验、网关显式选择 `comfy` 以及同一任务重复取图。隔离实例使用模拟 ComfyUI。Ubuntu 升级、跨机直连与 Qwen GPU 出图留待主机恢复后执行。

1. 在 Ubuntu 用 `ip -br -4 addr` 确认有线网卡地址，记录当前两个服务的状态和活动队列。队列空闲后备份现有配置，按[升级说明](configuration.md)更新部署目录，保留工作流、私有配置和任务数据。
2. 在实际的 `config/settings.toml` 设置 `[mcp]` 的 `host`。单物理网卡可用 `"lan"`，多地址主机填选定的具体私网 IPv4。在 Ubuntu 部署目录执行 `uv sync --locked --extra mcp`、`scripts/comfyui-service.sh ensure_ready` 和 `scripts/mcp-service.sh restart`。核对状态里的实际监听地址、进程归属和健康检查。
3. 在 Mac 的私有网关配置中将 `connections.comfy.base_url` 指向该地址的 `/mcp`，应用配置。先从 Mac 直接访问 Bridge，再经网关显式选择 `comfy` 生成 512×512 图片。记录任务号、尺寸、摘要、耗时和 ComfyUI 的实际 uni 采样记录。[验收命令](mcp.md#独立验收)

## 2026-11-22 · 0.9.2 自动防睡眠验收

- 使用隔离的任务库、真实 uni 编译器与模拟 ComfyUI 验证文生图和参考图编辑。休眠关闭、会话或电源总线不可访问时，任务正常提交并取回 PNG。确认定时休眠但授锁被拒时，上传数和 GPU 提交数均为零。桌面在授锁时退出的用例提交一次。
- 在回环网络上，由隔离 Maus 通过统一网关调用 Bridge。配置开启自动防睡眠，系统与会话电源总线不可访问，Comfy 请求交付到原消息卡。OpenAI 官方和 AiWave 路由也交付了模拟供应商的 PNG。[Maus 验收记录](https://github.com/hoholidayx/OpenMausBot/blob/main/docs/verification/async-image-mcp.md)
- logind 与 GNOME 两种正向策略和锁的判定使用离线用例。Ubuntu 主机当时 SSH 不可达，本次没有现场授锁或 GPU 回执。

## 2026-11-21 · 0.9.1 生图与运维边界验收

macOS 和 Ubuntu 临时目录分别通过 252 项包测试和 5 项客户端测试。替代运行时下的文生图与参考图编辑均取回示例 PNG；受管实例的就绪证据在入队后变化时，worker 在提交前返回 `backend_unavailable`。

Ubuntu 另用正式 `mcp-service.sh` 启动独立回环实例，以本地 HTTP 替身提供队列。保留上限设为 1 后，后台维护删除了较旧的失败任务，保留最近的失败任务与运行中任务，并轮转超限日志。独立状态检查、HTTP 健康检查和停止均通过。本轮使用临时配置和数据；生产部署与 GPU 出图记录见下方各版本验收。

## 2026-11-21 · 0.9.0 Ubuntu 隔离验收

在 Ubuntu 临时目录按锁文件安装候选版本。SDK、维护脚本、MCP 及发布检查共 251 项通过。额外用正式的 `mcp-service.sh` 在独立回环端口启动 MCP，后端队列使用本地替身。构造三个已结束任务和一个运行中任务，将保留上限设为 1。启动后的自动维护清除了两个旧任务，保留运行中任务和最新终态任务。已打开的日志描述符在轮转后继续写入当前文件。管理器核对进程、监听与健康检查后，正常停止了隔离实例。生产服务和用户任务未参与此次验收。

## 2026-11-21 · uni 原生 fast 和默认 quality

Ubuntu Bridge 0.8.2 通过回环 MCP 生成了四张 512×512 PNG。每次编辑都把同档文生图的返回值作为唯一参考图，将白色背景改为黄色，同时保留红方块和蓝球。耗时从提交到取回并校验图片，包含轮询。

| 请求 | 文生图 | 图生图 | ComfyUI 实际采样 |
| --- | ---: | ---: | --- |
| 显式 `fast` | 5.16 秒 | 9.20 秒 | 6 步，启用 Pruna 6-step v0.2 LoRA |
| 省略档位（默认 `quality`） | 11.17 秒 | 17.20 秒 | 40 步，关闭 LoRA |

四次执行均使用同一份 uni 的 Qwen Image 2.1 INT8、原生 KSampler、Euler/simple、CFG 1、denoise 1。逐条检查 ComfyUI history，采样器均执行了推理。MCP 重连、相同请求重放、冲突检测和重复取图均通过。客户端解码四张 PNG，并与回执的 SHA-256 核对一致。

另用仓库验收脚本的 `--generate --edit --preset fast --size 512x512` 复验。文生图 5.18 秒，使用生成图及调色参考图的双参考编辑 15.19 秒，均收到可解码的 PNG。

## 2026-11-21 · uni 默认 quality 实机验收

Ubuntu Bridge 0.8.1 使用同一个 uni 快照，客户端两次都省略 `preset`。从 MCP 提交到取回 512×512 PNG，文生图耗时 10.19 秒。用第一张作为唯一参考图，编辑耗时 18.21 秒。编辑保留红方块和蓝球，将白色背景改为黄色。两张图均已解码，并核对重复取图的 SHA-256。

两条 ComfyUI 执行记录均使用 Qwen Image 2.1 INT8、KSampler、Euler/simple、40 步、CFG 1，模型分支关闭加速 LoRA，图中没有 ViggleTurboSigmas。编辑执行记录包含一个 LoadImage 节点。此次通过 Ubuntu 回环地址访问 MCP，部署配置暂时关闭 GNOME 防睡眠组件。

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

当时的回退演练使用了部署前备份。接口和管理命令见 [MCP 部署与接入](mcp.md)。
