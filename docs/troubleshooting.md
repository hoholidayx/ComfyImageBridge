# 故障排查

## 工具没有出现

先确认 profile、两个插件是否启用、工具集是否允许、插件加载是否报错，再检查当前会话冻结的工具目录。工具历史裁剪不会删除已注册插件；旧会话 pin、延迟发现和模型没有调用工具是不同问题。兼容核心可以让 `comfy_image` 常驻；不会把全部 Ubuntu 管理工具都塞进模型前缀。

## `stale_copy` 或找不到服务端模块

确认两包来自同一提交，`remote_root` 指向部署的 Ubuntu 包，`.venv` 已用 `uv sync --locked` 创建。新的插件入口在远端导入 `comfy_workflow_api.workflow_bridge` / `runtime_bridge`，旧的临时目录不再被当作服务端实现来源。

先运行根检查脚本，按发行版重新部署 manifest 与客户端。不要只编辑服务端期望 hash 来掩盖尚未安装的客户端版本。

## 主机离线

`offline_unknown` 不等于已证实睡眠。只读 `ubuntu_status` 不发送 WoL；准备服务才会尝试有限唤醒。检查 LAN 配置、网卡 MAC、固件与系统 WoL、发送端网络和 known_hosts。没有 sudo 权限不应让工具擅自改电源配置。

## HTTP 正常但不出图

检查 `ready`、`suspect_stale_gpu`、`observed_at`、服务实例/恢复信息和渲染验证记录。唤醒后旧 GPU 上下文可能失效，HTTP 活着不能证明 GPU 可用。

任务返回 `stalled` 时看耗时、最后进度、日志与 GPU 观测；GPU 字段未知不能按 0% 处理。不要不断重新生成、清空全局队列或中断别人的任务。`comfy_task` 可查询/取消本任务；无法正常结束时才使用管理诊断/恢复流程。

`recover` 只针对身份核对过的受管实例，信号超时自动升级并写回执。活动队列正常时不应主动恢复。出现内核 GPU reset、KFD 错误或传感器 EBUSY 时，服务重启也未必能修复驱动；本工具不会自动重启整机或变更 ROCm。

## 启动失败或端口冲突

用 `scripts/comfyui-service.sh status` 查看 PID/启动标识、LISTEN 归属和日志。端口占用只以 LISTEN 证据判断，不能把 TIME-WAIT 当成另一个服务。外部进程占用或 PID 复用时管理器拒绝误杀。

查看原 `run_comfyui.sh`、uv 预检、HSA/MIOpen 与日志。不要修改 ComfyUI 的 `uv.lock` 或受保护依赖来消除包装层报错，也不要重复拉起第二个 ComfyUI。

## 主机不自动睡眠

只读检查 `uv run comfy-workflow sleep-guard status` 与 GNOME/系统 inhibitors、队列及 lease 持有者。请求/队列结束后仍未超过 10 秒宽限期时，短暂持锁正常。lease 与任务进程身份关联；不能仅凭年龄或 GPU 0% 强制删除。

队列未知、远端不可达、取消未结束或调用进程仍存活时先诊断。自动休眠还取决于用户会话是否空闲、桌面策略和其他应用 inhibitor。模块不会修改 GNOME 自动休眠策略。

## 输出缺失、回图失败或恢复任务

保留原 `task_id` 查询，不重复提交。Ubuntu `data/runs/` 内保存请求、执行图、提交响应、history 与结果；批次和插件回执位于各自的数据目录。提交结果不确定时不要假设失败。

输出可能被数量清理移除，此时索引/标题不代表原文件仍存在。回图失败还应核对平台适配与当前会话来源绑定，不要用跨会话图片 ID 或在日志里公开用户内容。
