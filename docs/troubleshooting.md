# 故障排查

MCP 部署及工具错误见 [MCP 接入](mcp.md)。先用 `scripts/mcp-service.sh status` 和 `/healthz` 检查 MCP 进程，再单独用 `scripts/comfyui-service.sh status` 检查渲染就绪。对于提交结果不确定的任务，框架继续用原任务 ID 查询。

## HTTP 正常但不出图

检查 `ready`、`suspect_stale_gpu`、`observed_at`、服务实例/恢复信息和渲染验证记录。唤醒后旧 GPU 上下文可能失效，HTTP 活着不能证明 GPU 可用。

任务返回 `stalled` 时看耗时、最后进度、日志与 GPU 观测；GPU 字段未知不能按 0% 处理。不要不断重新生成、清空全局队列或中断别人的任务。`comfy_task` 可查询/取消本任务；无法正常结束时才使用管理诊断/恢复流程。

`recover` 只针对身份核对过的受管实例，信号超时自动升级并写回执。活动队列正常时不应主动恢复。出现内核 GPU reset、KFD 错误或传感器 EBUSY 时，服务重启也未必能修复驱动；本工具不会自动重启整机或变更 ROCm。

## 编辑尺寸与原图不一致

编辑 `size=auto` 跟随第一张参考图经过服务端缩放、对齐后的尺寸，不保证与原始 PNG 的像素宽高相同。检查SDK 执行记录中的 `output_size` 应为 `{"mode":"follow_image"}`，执行图中 latent 开关应选择 `TextEncodeQwenImage21` 的尺寸输出。0.6.4 修复了客户端把 `auto` 改写为原图固定宽高的问题；已有历史任务不会被重写或重新提交。

固定尺寸与跟随尺寸在相同宽高下的对照均可能正常，不能仅凭分支选择认定花屏原因。诊断时应固定参考图、提示词、seed、模型与采样设置，并同时核对参考图处理尺寸和实际采样尺寸。

## 生成图再次 edit 后纹理劣化

Qwen Image 2.1 当前配置中，若参考图由某个 seed 生成，再用相同 seed 编辑它，已在合成图交叉对照中复现斑驳、硬边和纹理劣化。先核对参考图的生成 seed 与本次实际 `seed_used`；避免沿用来源 seed，不要把此现象直接认定为文件上传、latent 尺寸或所有模型的通病。

MCP 从返回图片的原始字节校验 SHA-256 并登记实际 seed。手动 ComfyUI、直接 SDK / CLI 调用时自行选择新的编辑 seed。

## 启动失败或端口冲突

用 `scripts/comfyui-service.sh status` 查看 PID/启动标识、LISTEN 归属和日志。端口占用只以 LISTEN 证据判断，不能把 TIME-WAIT 当成另一个服务。外部进程占用或 PID 复用时管理器拒绝误杀。

查看原 `run_comfyui.sh`、uv 预检、HSA/MIOpen 与日志。不要修改 ComfyUI 的 `uv.lock` 或受保护依赖来消除包装层报错，也不要重复拉起第二个 ComfyUI。

## 主机不自动睡眠

只读检查 `uv run comfy-workflow sleep-guard status` 与 GNOME/系统 inhibitors、队列及 lease 持有者。请求/队列结束后仍未超过 10 秒宽限期时，短暂持锁正常。lease 与任务进程身份关联；不能仅凭年龄或 GPU 0% 强制删除。

队列未知、远端不可达、取消未结束或调用进程仍存活时先诊断。自动休眠还取决于用户会话是否空闲、桌面策略和其他应用 inhibitor。模块不会修改 GNOME 自动休眠策略。

## 输出过期与清理

用原任务 ID 查询 `image_status`。超过结果上限的任务返回 `expired`，再次提交相同请求仍复用原 ID。检查 `data/cleanup.log` 和管理器日志。文件清理预览使用 `uv run comfy-workflow cleanup --dry-run`。
