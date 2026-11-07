# 故障排查

MCP 部署及工具错误见 [MCP 接入](mcp.md)。先用 `scripts/mcp-service.sh status` 和 `/healthz` 检查 MCP 进程，再单独用 `scripts/comfyui-service.sh status` 检查渲染就绪。对于提交结果不确定的任务，框架继续用原任务 ID 查询。

以下涉及插件、profile 或 SSH 生图的条目供 Hermes 回退使用。

## 工具没有出现

先确认 profile、两个插件是否启用、工具集是否允许、插件加载是否报错，再检查当前会话冻结的工具目录。工具历史裁剪不会删除已注册插件；旧会话 pin、延迟发现和模型没有调用工具是不同问题。兼容核心可以让 `comfy_image` 常驻；不会把全部 Ubuntu 管理工具都塞进模型前缀。

目录存在、启用配置和独立 CLI 加载成功不代表运行中的网关已注册工具。查看当前启动之后的 `logs/agent.log` 或 `logs/errors.log`；`load timed out after 10s` 表示本次注册已撤销。0.6.3 将桌面消费者改为通过 `register_gateway_ready` 延后启动，避免插件注册线程与正在导入的网关互等。需要同步升级对应 Hermes 核心、客户端与 Ubuntu 客户端契约，再重启网关并核对实时注册；单纯新建会话或提高超时不能修复导入互等。

## `stale_copy` 或找不到服务端模块

确认两包来自同一提交，`remote_root` 指向部署的 Ubuntu 包，`.venv` 已用 `uv sync --locked` 创建。新的插件入口在远端导入 `comfy_workflow_api.workflow_bridge` / `runtime_bridge`，旧的临时目录不再被当作服务端实现来源。

先运行根检查脚本，按发行版重新部署 manifest 与客户端。不要只编辑服务端期望 hash 来掩盖尚未安装的客户端版本。

## 主机离线

`offline_unknown` 不等于已证实睡眠。只读 `ubuntu_status` 不发送 WoL；准备服务才会尝试有限唤醒。检查 LAN 配置、网卡 MAC、固件与系统 WoL、发送端网络和 known_hosts。没有 sudo 权限不应让工具擅自改电源配置。

## HTTP 正常但不出图

检查 `ready`、`suspect_stale_gpu`、`observed_at`、服务实例/恢复信息和渲染验证记录。唤醒后旧 GPU 上下文可能失效，HTTP 活着不能证明 GPU 可用。

任务返回 `stalled` 时看耗时、最后进度、日志与 GPU 观测；GPU 字段未知不能按 0% 处理。不要不断重新生成、清空全局队列或中断别人的任务。`comfy_task` 可查询/取消本任务；无法正常结束时才使用管理诊断/恢复流程。

`recover` 只针对身份核对过的受管实例，信号超时自动升级并写回执。活动队列正常时不应主动恢复。出现内核 GPU reset、KFD 错误或传感器 EBUSY 时，服务重启也未必能修复驱动；本工具不会自动重启整机或变更 ROCm。

## 编辑尺寸与原图不一致

编辑 `size=auto` 跟随第一张参考图经过服务端缩放、对齐后的尺寸，不保证与原始 PNG 的像素宽高相同。检查客户端任务及 worker 的 `output_size` 应为 `{"mode":"follow_image"}`，执行图中 latent 开关应选择 `TextEncodeQwenImage21` 的尺寸输出，fast 的 sigma 节点使用同一采样 latent。0.6.4 修复了客户端把 `auto` 改写为原图固定宽高的问题；已有历史任务不会被重写或重新提交。

固定尺寸与跟随尺寸在相同宽高下的对照均可能正常，不能仅凭分支选择认定花屏原因。诊断时应固定参考图、提示词、seed、模型与采样设置，并同时核对参考图处理尺寸和实际采样尺寸。

## 生成图再次 edit 后纹理劣化

Qwen Image 2.1 当前配置中，若参考图由某个 seed 生成，再用相同 seed 编辑它，已在合成图交叉对照中复现斑驳、硬边和纹理劣化。先核对参考图的生成 seed 与本次实际 `seed_used`；避免沿用来源 seed，不要把此现象直接认定为文件上传、latent 尺寸或所有模型的通病。

客户端0.6.5的 edit 默认随机，并校验所有参考图的已知直接生成 seed；显式冲突也会自动换值。查看 `seed_policy` 可区分请求值、实际值、自动调整原因与未知来源数量。批量请求保持连续 seed，整段区间通过校验后才提交。文生图仍默认固定0。

来源索引按当前 profile 保存文件 SHA256 和实际生成 seed，改名或复制且保留原始字节的文件仍可匹配。重新压缩或修改 PNG 元数据后，若新哈希未登记则会标记为未知来源。未知来源图允许编辑，显式 seed 未查到已知冲突时保留。手动 ComfyUI、直接 SDK/CLI 调用须自行使用新 seed。

删除生成图或大回执不会删除轻量 hash/seed 来源记录。数据库不可读属于校验故障，应先修复，不能通过删库把已知来源降级为未知。已提交任务只查询/恢复原任务，不因自动换 seed 重新生成。

## 启动失败或端口冲突

用 `scripts/comfyui-service.sh status` 查看 PID/启动标识、LISTEN 归属和日志。端口占用只以 LISTEN 证据判断，不能把 TIME-WAIT 当成另一个服务。外部进程占用或 PID 复用时管理器拒绝误杀。

查看原 `run_comfyui.sh`、uv 预检、HSA/MIOpen 与日志。不要修改 ComfyUI 的 `uv.lock` 或受保护依赖来消除包装层报错，也不要重复拉起第二个 ComfyUI。

## 主机不自动睡眠

只读检查 `uv run comfy-workflow sleep-guard status` 与 GNOME/系统 inhibitors、队列及 lease 持有者。请求/队列结束后仍未超过 10 秒宽限期时，短暂持锁正常。lease 与任务进程身份关联；不能仅凭年龄或 GPU 0% 强制删除。

队列未知、远端不可达、取消未结束或调用进程仍存活时先诊断。自动休眠还取决于用户会话是否空闲、桌面策略和其他应用 inhibitor。模块不会修改 GNOME 自动休眠策略。

## 输出缺失、回图失败或恢复任务

保留原 `task_id` 查询，不重复提交。Ubuntu `data/runs/` 内保存请求、执行图、提交响应、history 与结果；批次和插件回执位于各自的数据目录。提交结果不确定时不要假设失败。

输出可能被数量清理移除，此时索引/标题不代表原文件仍存在。回图失败还应核对平台适配与当前会话来源绑定，不要用跨会话图片 ID 或在日志里公开用户内容。

桌面报 `No verified bot binding` 时，核对生图插件 0.6.3 与具备持久完成通知及网关就绪回调 API 的 Hermes 核心是否成套更新。只补 `bot=local` 会遗漏本地投递消费者和会话隔离。桌面新任务固定绑定创建时的逻辑会话；删除该会话后 `delivery_state=cancelled` 是预期结果，切换到其他聊天不会收到旧任务图片。压缩与临时断开不会删除绑定；暂不可达时只重放原事件，不重提生成。
