# 开发与升级

## 本地验证

在仓库根目录运行：

```bash
uv run --project ubuntu --locked --extra mcp python scripts/check.py
```

脚本以锁定环境运行 Ubuntu MCP、SDK、管理器及快照测试与独立 MCP 客户端。测试使用本机替身或回环连接，不需要 GPU、SSH 或运行中的 ComfyUI。首次重建 uv 环境可能需要下载依赖。

单独验证 Ubuntu 包：

```bash
uv run --project ubuntu --locked --extra mcp python ubuntu/scripts/check.py
```

真实生成属于显式验收步骤，请使用几何示例，并确认现场 ComfyUI/GPU 正常。当前打包验证不会擅自执行真实生成、唤醒、关机或重启。

## 代码位置

| 修改目标 | 位置 |
| --- | --- |
| MCP 工具协议与生产服务组装 | `ubuntu/src/comfy_workflow_api/mcp_server.py` |
| 任务队列、去重、seed 来源与结果保留 | `image_jobs.py`、`retention.py` |
| SDK 公开入口、执行流程与编译 | `api.py`、`generation.py`、`seed_policy.py`、`compiler.py` |
| 运行时接口与 Ubuntu 实现 | `backend.py`、`operations/runtime.py` |
| 只读就绪检查与真实烟测 | `operations/service_readiness.py`、`operations/render_probe.py` |
| 服务启停、睡眠恢复和定时清理 | `ubuntu/scripts/comfyui-service.py`、`ubuntu/scripts/mcp-service.sh`、`sleep_cycle.py`、`operations/maintenance.py` |
| 快照读取与允许拓扑 | `snapshots.py`、`ubuntu/workflows/` |

表中 Python 模块的相对路径从 `ubuntu/src/comfy_workflow_api/` 起算。修改模块边界后，运行上面的离线检查。`ubuntu/tests/test_generation_boundaries.py` 验证两侧可以独立加载，并使用替代运行时覆盖文生图和参考图编辑。依赖方向和提交时序见[架构说明](architecture.md#生图与-ubuntu-运维的边界)。

`ubuntu/tests/test_edit_seed_policy.py` 从 SDK、CLI 和 MCP 任务入口捕获替身 `/prompt`，核对固定输入 seed 的两次编辑使用不同实际节点值、回执一致、重放不重抽，以及文生图固定 seed 不变。`compile` 的编辑预览独立随机，不作为后续提交的 seed 预约；低层 `compile_graph` 不负责随机化。

## 发布步骤

1. 保持 Python 3.12 和独立 uv 环境。SDK 基础安装使用标准库，MCP 依赖放在 `mcp` 可选组。
2. 更新 Ubuntu 包版本和必要文档，运行根检查脚本与 `git diff --check`。部署后的真实生成使用[独立 MCP 验收入口](mcp.md#独立验收)，结果记入[实机验收记录](mcp-rollout.md)。
3. 发布前核对内容只包含源码、文档和合成示例，排除用户实际配置、图片、任务、模型及密钥。

工作流修改通过显式同步产生新快照；同步前先从 ComfyUI 保存工作流，再在测试环境校验新拓扑与采样通路。分发前去掉用户默认提示词和参考图名，并更新内容 hash。更换拓扑不能只复制旧 allowlist。

## 数据迁移

本次整理不迁移旧图片或历史任务。若后续需要迁移，应单独备份数据与实际配置，保持任务 ID、服务端文件命名空间及权限一致；运行中的 worker 和防休眠 lease 不能当成可直接迁移的静态文件。不要把运行态数据作为 Git 版本升级的一部分。
