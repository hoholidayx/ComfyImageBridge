# 工作约定

- `hermes/` 是客户端插件包；`ubuntu/` 是 Ubuntu 工作流 SDK、CLI 和服务管理包。服务端实现只放在 Ubuntu 包，客户端通过固定 SSH 入口调用。
- 两端 Python 均用各自 `uv.lock` 管理，Python 3.12；不添加 PyTorch、ROCm 或 ComfyUI 运行时依赖。
- ComfyUI 必须通过用户现有 `run_comfyui.sh` 启动，先阅读脚本与 Ubuntu 包约定；不得绕过 HSA/MIOpen/uv 配置或修改受保护依赖。
- 后台进程须脱离调用会话、关闭 stdin、重定向输出，提供启动、停止、重启和状态检查；发信号前核对 PID、启动标识与端口归属。不新增 systemd/launchd 自启配置。
- 未明确要求时，验证只运行离线测试，不唤醒主机、不重启服务、不提交真实生成任务。
- 不提交真实配置、SSH 密钥、用户提示词、聊天记录、参考图、生成图、历史任务或回执。示例仅使用合成几何图片和文档示例地址。
- 改动客户端运行代码或 `plugin.yaml` 后，更新版本并运行 `hermes/scripts/export_contract.py`；用根目录 `scripts/check.py` 核对跨包契约。
- 修改参数与工具行为时同步中文文档。不要重新开放任意步数、CFG 或负向提示词；当前 fast 为 LoRA/6 步，quality 为无 LoRA/25 步，CFG=1。
