# Comfy Workflows 0.6.5 / contract 7

安装、配置、环境和双端同步见[Hermes 包说明](../../README.md)。此目录只包含 Hermes 客户端；`remote_bridge.py` 是薄入口，服务端实现在独立 Ubuntu 包的 `comfy_workflow_api.workflow_bridge` 中。

## 统一入口

`comfy_image` 统一文生图与多图编辑。主模型负责交互、提示词、参考图顺序和档位；程序在后台完成 Ubuntu 唤醒、服务就绪、提交、查询、下载和校验。正常任务不需要模型轮询。

```json
{"prompt":"雨夜街角撑伞的白色几何机器人，全身构图","title":"雨夜机器人","preset":"fast","size":"864x1536"}
```

```json
{"prompt":"以 <image1> 为编辑底图，保持其中机器人的形状、姿态与构图；仅采用 <image2> 的橙黄色配色，不引入其中的物体或背景。","title":"雨夜机器人配色精修","images":["img_000001","/absolute/reference.png"],"size":"auto"}
```

| 参数 | 默认与约束 |
|---|---|
| prompt | 必填，非空，最多8000字符，超限报错 |
| preset | 默认fast：LoRA/6步/Pruna自定义sigmas；仅用户明确要求时选择quality：无LoRA/25步/Euler simple |
| images | 可省略或传空数组，有序0–10张本机PNG或图片ID；有编辑底图时将其放在第1项 |
| size | auto：编辑 latent 跟随第一张参考图处理后的尺寸、文生图1024×1024；支持明确指定比例或WxH |
| title | 可选，最多80字符，描述生成意图，非视觉核验结论 |
| seed | 文生图默认固定0；edit默认随机，显式冲突自动替换并告知；无符号64位整数 |
| count | 默认1，范围1–4，各张seed递增 |

aiwave/grok-4.3 会拒绝 seed 含超大整数 `maximum` 的工具 schema，因此模型工具 schema 将无符号64位上限写入描述；运行时仍严格校验 `0 ≤ seed` 且 `seed + count - 1 < 2^64`。

指定尺寸64–2048且为32的倍数；比例模式沿用1MP/32对齐，实际尺寸写回记录。精确9:16可用576x1024或864x1536。CFG固定1、负向为空，不接受任意steps/cfg/use_lora/negative_prompt。

编辑默认保留 `size=auto`，请求、后台 worker 与 SSH 提交全程传递 `output_size={"mode":"follow_image"}`。第一张参考图在服务端按默认约 1024×1024 的像素面积等比缩放并做 32 对齐，采样 latent 和 fast 的 sigma 分辨率偏移共同跟随该处理结果。原始 PNG 宽高无需预先满足输出尺寸限制；输出也不保证保持原始像素宽高。只有明确要求其他画布尺寸时才指定比例或 WxH。

## Qwen Image 2.1 编辑 seed 保护

**生成图作为参考图再次 edit 时，避免沿用它的生成 seed。** 当前模型配置的合成图对照已复现同源 seed 触发纹理劣化；这是一条工作流使用约束，不是所有模型的通用结论。

客户端复用当前 profile 的 `jobs.sqlite3`，为所有 Qwen 文生图和编辑输出登记文件 SHA256 与实际生成 seed。同一图片可以对应多个已知生成 seed。新 `comfy_image` 和兼容 `comfy_edit` 共用保护：

- edit 省略 seed：随机选择基值，避开所有参考图的已知直接生成 seed。
- 显式 seed 冲突：自动选择新基值；`seed_used` 返回实际值，`seed_policy` 记录请求值、实际值、选择原因和未知来源数量。
- `count>1`：保持逐张 `seed+i`，整个连续区间都不得与已知参考图 seed 冲突。
- 未知来源：允许编辑并标记未知。省略 seed 时随机；显式指定且未查到冲突时保留，不能保证它没有未知冲突。
- 已选值在远端提交前保存；恢复与重试复用该值。已尝试提交的任务不换 seed、不自动重生。数据库故障会停止提交，不伪装成来源未知。

兼容入口在 SSH 提交前写入轻量 `seed-intents` 记录，仅含实际 seed 策略、输入哈希与数量，不保存图片或提示词；远端回执落盘后移除。响应丢失时保留该记录，并返回实际 `seed_used` 与 `seed_intent_id`，不能因未收到回复而自动重提。

SHA256 按整个文件的原始字节计算，改名和字节相同的副本可以匹配。重新压缩、修改元数据或调整像素都会改变文件字节，哈希未登记时按未知来源处理。

索引校验当前参考图的直接生成 seed。旧产物与旧回执会幂等补录；文件已过期但有可靠 hash/seed 时仍保留来源记录。图片和大回执清理不会删除该索引。直接在 ComfyUI 或 Ubuntu SDK/CLI 操作不经过客户端保护，应自行换用新 seed。

## 参考图与提示词

遵循 [Qwen Image 2.1 官方提示词规则](https://raw.githubusercontent.com/QwenLM/Qwen-Image-2.1/main/prompt_rewrite/prompts/system_prompt_edit.txt)：

- 0张：省略 `images` 或传 `[]`，使用纯文生图提示词，不写图片标签。
- 1张：自然称“这张图”或“图中的机器人”，不需要图片标签。
- 2–10张：按 `images` 数组从1开始使用 `<image1>`、`<image2>` 等标签，不加下划线；逐图说明取用哪些属性、保留哪些内容。不要把节点槽名 `image_1` 写成提示词标签 `<image_1>`。

编辑已有画面时，将底图放在 `images[0]` 并在提示词中明确其底图角色，其余图分别说明参考用途。若是没有底图的新场景合成，则明确每张图提供的人物、物体、风格等参考及其保留属性，并显式指定 `size`，避免默认继承第一张图的画布尺寸。程序保留图片数组顺序和原始提示词，不自动改写提示词。

## 执行与回传

工具保存任务、输入副本和可信路由，快速返回任务ID。有限独立worker继续执行；Telegram 投递泵直接更新进度、发送图片和异常处理按钮。桌面投递泵使用 Hermes 核心的持久完成通知接口，把图片和说明写入原会话，实时显示并可从历史重放。两条路径均不调用 LLM 或 inject_message。

桌面任务创建时由核心签发固定投递目标，绑定 profile 和原逻辑会话。切换窗口、历史压缩或网关重启均不改投；原会话已删除时，取消该任务后续投递并记录 `delivery_state=cancelled`，生成任务本身不会因此被重复提交或自动中止。核心先验证并保留图片副本，原下载文件后续消失不影响已接受内容的重放。

本地任务查询、图片 ID、重发和标题卡均按逻辑会话隔离；压缩只改变历史存储 tip，不改变归属。Telegram 保持原用户、聊天、线程和 bot 的范围，按钮 24 小时过期且一次消费。未升级核心时，桌面新后台任务会在启动 worker 前明确报错；无网关的独立 CLI 仍可使用旧提交/取图接口和诊断命令。

任务不自动重生；已确认失败后显式 retry 才创建新尝试。提交状态不确定只能查询，不自动重提。

```text
/comfy status <task_id>
/comfy cancel <task_id>
/comfy retry <task_id>
/comfy resend <image_id>
/comfy images 雨夜机器人
/comfy run {"prompt":"几何机器人","title":"机器人","size":"512x512"}
```

网关重启只中断投递，worker继续运行，重连后扫描outbox。桌面按原目标和稳定 event ID 重放同一完成通知，避免丢回执后重复显示；Telegram 发送响应不确定时仍标记 delivery_uncertain，须显式 resend。resend 只补发已有图片，不重新生成。观察进程恢复不得重提生成。

后台worker提供start/stop/restart/status脚本，记录日志、PID、启动标识和命令；信号前核验归属。stop仅停止观察，不等同取消Comfy任务。无新增launchd/systemd配置。

```sh
COMFY_WORKER_PYTHON=/Hermes运行时/python /插件目录/scripts/comfy-worker.sh status TASK_UUID DATA_DIR
# 或
/Hermes运行时/python /插件目录/worker.py --action status --task-id TASK_UUID --data-dir DATA_DIR
```

DATA_DIR为当前profile的plugin-data/comfy-workflows；运行中的worker信息包含实际Python路径和日志位置。restart只恢复观察，不重新生成。

## 图片卡与上下文

产物索引保存图片ID、标题、参考关系、实际尺寸、seed、档位和receipt。完成时直接发图片及标题；下一次正常聊天通过pre_llm_call补入尚未登记的图片卡，最多4张、1600字符。卡片的api_content稳定回放；tool_history_echo=false继续裁剪旧工具记录。

```text
img_000001｜雨夜机器人
fast · 864×1536 · seed 0
```

标题由主模型原调用顺手给出；缺失时机械提取提示词，不额外调用模型。标题是生成意图，需要确认实际细节时可读图。去重需确认卡片已进入历史，不能hook返回就推进游标。新会话只补最近4张，旧产物按标题查询。

图片沿用现有数量上限与专属目录清理；轻量索引保留，文件消失标expired且不可再作参考。完整提示词、执行图、哈希、采样和运行日志留在receipt，不反复返回模型。

## 常驻与兼容

Hermes通用配置，新会话生效：

```yaml
tools:
  tool_search:
    enabled: auto
    eager: [comfy_image]
agent:
  tool_history_echo: false
```

只常驻统一入口。comfy_task按需查询、历史和取消；原comfy_generate、comfy_edit、comfy_tasks、comfy_workflows保留一个兼容版本。ubuntu-runtime仍负责进程归属、唤醒烟测和原脚本启动，新入口内部复用，无需模型先调用comfy_service。

SDK/CLI旧参数及历史任务保持兼容。旧生成接口沿用原就绪流程，正常使用应迁移到comfy_image。版本/哈希不匹配拒绝新提交，历史查询仍可用。插件代码的版本/哈希通过Ubuntu的config/client-contract.json显式同步，服务端不再保存插件源码副本；重启网关后以新会话验证新工具目录。
