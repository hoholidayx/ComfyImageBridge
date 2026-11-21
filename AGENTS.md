# 工作约定

- `ubuntu/` 持有工作流 SDK、CLI、内网 MCP 生图服务和本机运维。外部客户端通过标准 MCP 协议接入。
- Ubuntu 包使用 Python 3.12 和 `ubuntu/uv.lock`；不添加 PyTorch、ROCm 或 ComfyUI 运行时依赖。
- ComfyUI 必须通过用户现有 `run_comfyui.sh` 启动，先阅读脚本与 Ubuntu 包约定；不得绕过 HSA/MIOpen/uv 配置或修改受保护依赖。
- 后台进程须脱离调用会话、关闭 stdin、重定向输出，提供启动、停止、重启和状态检查；发信号前核对 PID、启动标识与端口归属。仅明确授权后新增系统托管配置；用户于 2026-11-06 已授权本项目的硬件监控、邮件报警与睡眠恢复 systemd 单元。
- 未明确要求时，验证只运行离线测试，不唤醒主机、不重启服务、不提交真实生成任务。
- 不提交真实配置、SSH 密钥、用户提示词、聊天记录、参考图、生成图、历史任务或回执。示例仅使用合成几何图片和文档示例地址。
- 修改参数与工具行为时同步中文文档。不要重新开放任意步数、CFG 或负向提示词；当前 fast 为 LoRA/6 步，默认 quality 为无 LoRA/40 步，CFG=1。

## Qwen uni 工作流

- 本项目只维护 `qwen-image-2.1-unified.json`。文生图和编辑保留各自的接口别名，共用一份快照。仓库的 `ubuntu/workflows/` 和 Bridge 部署的 `workflows/` 只放当前 uni 及清单。测试导出、单档位副本和部署临时文件用后清理。清理限于 Bridge 项目和部署目录，保留 ComfyUI 用户工作流、自定义节点及其他项目文件。
- 两个入口和两个档位均使用 uni 原生 KSampler、Euler/simple、CFG 1、denoise 1。默认 quality 关闭加速 LoRA、运行 40 步，fast 启用加速 LoRA、运行 6 步。
- 改动工作流、预设或编译器后，离线比较同一输入的两档执行图。获准实机验收时，用 MCP 分别跑 fast 和默认 quality 的文生图和图生图，解码 PNG，并核对 ComfyUI 执行记录中的采样器、步数、LoRA 分支和参考图数。
