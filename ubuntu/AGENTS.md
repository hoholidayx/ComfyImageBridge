# Ubuntu 端维护约定

- MCP 只提供生图提交、状态和取图。监听指定可信内网地址，校验 Host 和 Origin。启动、唤醒和恢复由独立运维入口负责，MCP 仅做只读就绪检查。
- ComfyUI 通过 `config/settings.toml` 的 `service.run_script` 启动；默认是 `service.comfy_dir` 下的 `run_comfyui.sh`。运行前先读脚本实时内容，保留其中 HSA、MIOpen、uv 及其他设备配置。
- 不得绕过原脚本直接执行 `python main.py`，不得修改 ComfyUI 的 uv.lock 或受保护的 PyTorch、ROCm 依赖。SDK 使用自己的 uv 环境，不需要这些依赖。
- 原启动脚本不一定有 stop/restart 参数，不得虚构。管理操作使用本包 `scripts/comfyui-service.sh`，先核实进程归属与启动标识，禁止按进程名批量结束。
- 后台实例必须独立于 SSH 和工具会话，记录 PID、启动标识、命令和日志，正确处理 stdin/stdout/stderr、进程组及 SIGHUP。启动后从独立连接复核 PID、LISTEN 端口和生成烟测。
- `status` 只读；HTTP 应答不等于 GPU 可推理。恢复后要以当前进程、恢复纪元和工作流版本对应的真实烟测确认 ready。GPU 指标不可读时保留 unknown，不当作 0%。
- 普通停止不会中断健康的活动生成。恢复仅处理已验证归属的异常实例；优雅停止超时按既有策略升级信号，不结束无归属进程。
- 防睡眠 lease 关联持有进程身份；年龄只报警，不能按时间强行释放活跃 lease。队列清空且无持有者后按宽限期释放。
- 仅在明确要求系统托管或登录自启时引入 systemd 等常驻配置。整理代码或读取文档不代表授权启动、唤醒、生成或重启服务。
