# Ubuntu 端维护约定

- MCP 只提供生图提交、状态和取图。监听指定可信内网地址，校验 Host 和 Origin。启动、唤醒和恢复由独立运维入口负责，MCP 仅做只读就绪检查。
- ComfyUI 通过 `config/settings.toml` 的 `service.run_script` 启动；默认是 `service.comfy_dir` 下的 `run_comfyui.sh`。运行前先读脚本实时内容，保留其中 HSA、MIOpen、uv 及其他设备配置。
- 不得绕过原脚本直接执行 `python main.py`，不得修改 ComfyUI 的 uv.lock 或受保护的 PyTorch、ROCm 依赖。SDK 使用自己的 uv 环境，不需要这些依赖。
- 原启动脚本不一定有 stop/restart 参数，不得虚构。管理操作使用本包 `scripts/comfyui-service.sh`，先核实进程归属与启动标识，禁止按进程名批量结束。
- 后台实例必须独立于 SSH 和工具会话，记录 PID、启动标识、命令和日志，正确处理 stdin/stdout/stderr、进程组及 SIGHUP。启动后从独立连接复核 PID、LISTEN 端口和生成烟测。
- `status` 只读；HTTP 应答不等于 GPU 可推理。恢复后要以当前进程、恢复纪元和工作流版本对应的真实烟测确认 ready。GPU 指标不可读时保留 unknown，不当作 0%。
- 普通停止不会中断健康的活动生成。恢复仅处理已验证归属的异常实例；优雅停止超时按既有策略升级信号，不结束无归属进程。
- 防睡眠是 Ubuntu 运维适配。只有实测 logind 或当前 GNOME 电源档位启用了定时休眠，提交和取图才要求获得对应 inhibitor。策略关闭、探测超时或不可读时继续生图。已进入挂起或恢复的门禁独立生效。
- 防睡眠 lease 关联持有进程身份；年龄只报警，不能按时间强行释放活跃 lease。队列清空且无持有者后按宽限期释放。
- 仅在明确要求系统托管或登录自启时引入 systemd 等常驻配置。整理代码或读取文档不代表授权启动、唤醒、生成或重启服务。

## 模块边界

- `image_jobs.py` 和 `generation.py` 通过 `GenerationRuntime` 获取就绪、提交保护和实例证据。Ubuntu 适配写在 `operations/runtime.py`，服务启停、挂起恢复和 GPU 探测留在运维入口。
- 运维烟测与生图共用快照校验、编译器和 ComfyUI 客户端，单独保存烟测状态。新增影响诊断图或就绪判定的依赖时，同步检查 `operations/service_readiness.py` 的 `workflow_hash` 和边界回归测试。
- 任务结果保留逻辑放在 `retention.py`，全局清理与日志轮转放在 `operations/`；生产 MCP 入口负责组装清理线程与 worker 的生命周期。

## Qwen uni 工作流

- 本项目只维护 `qwen-image-2.1-unified.json`。文生图和编辑保留各自的接口别名，共用一份快照。仓库的 `ubuntu/workflows/` 和 Bridge 部署的 `workflows/` 只放当前 uni 及清单。测试导出、单档位副本和部署临时文件用后清理。清理限于 Bridge 项目和部署目录，保留 ComfyUI 用户工作流、自定义节点及其他项目文件。
- 两个入口和两个档位均使用 uni 原生 KSampler、Euler/simple、CFG 1、denoise 1。默认 quality 关闭加速 LoRA、运行 40 步，fast 启用加速 LoRA、运行 6 步。
- 改动工作流、预设或编译器后，离线比较同一输入的两档执行图。获准实机验收时，用 MCP 分别跑 fast 和默认 quality 的文生图和图生图，解码 PNG，并核对 ComfyUI 执行记录中的采样器、步数、LoRA 分支和参考图数。
