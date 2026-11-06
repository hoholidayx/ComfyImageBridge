# Hermes 核心兼容性

本插件包整理自已经运行过的 Hermes 扩展环境。已知核心源码为 [hoholidayx/hermes-agent](https://github.com/hoholidayx/hermes-agent) 提交 `2de4c987400d7753be523b4657449c4707b9df09`。这是一份兼容基线，不表示已验证任意上游版本。

## 必要接口

- 插件上下文 `get_config`、`register_tool`、`register_cli_command` 等标准注册能力；配置读取正式路径为 `plugins.entries.<plugin_id>.settings`。
- 带范围的原生命令、`register_platform_handler` 和 `pre_llm_call` hook，用于直接回调、图片索引与标题卡。
- 可信会话来源与路由绑定，包括 `bound_session_key`、`bound_route` 和 `source_transport_ref`；直接投递必须回到原会话，不能以模型提供的聊天 ID 代替。
- 现有 Telegram 适配器，用于图片主动推送、选项按钮和不经过模型的命令处理。CLI 主要用于调用/诊断；不保证其他平台拥有 Telegram 相同交互能力。

缺少接口时请检查插件加载日志和诊断结果，先确定差异，再适配对应核心；不要删除鉴权、会话绑定或回调范围限制来让导入通过。本仓库安装器不会修改 Hermes 核心。

## 可选核心设置

```yaml
tools:
  tool_search:
    enabled: auto
    eager: [comfy_image]
agent:
  tool_history_echo: false
```

`eager` 让常用生图工具定义常驻，其他管理工具仍按当前策略发现。它不赋予被 profile/会话禁用工具的调用权。是否生效还受当前会话冻结的工具策略影响，修改后使用新会话验证。

`tool_history_echo` 控制主聊天后续请求如何投影旧工具历史，不改变磁盘原始历史或插件注册。标题卡只补充有限的生成意图信息，不能代替直接看图。

这两个选项由上述核心提供，不是插件自身的实现。使用不支持的核心时不要仅复制 YAML 便认为功能已经生效。

## 升级验证顺序

1. 确认实际 Hermes profile、Python 与网关运行目录。
2. 确认两个插件加载成功、相关工具集允许调用、必要 hook/命令注册成功。
3. 用 `comfy_workflows` 或插件 CLI 检查后端版本、契约和 `stale_copy`。这一步需要 SSH 到在线 Ubuntu，但不应生成图片。
4. 在新会话确认 `comfy_image` 的可见性；再进行显式的真实生成验收。

仓库的离线测试使用替身上下文与模拟传输来验证插件逻辑；不能替代真实网关、平台适配和 GPU 验收。普通代码整理不会自动重新部署或重启用户网关。
