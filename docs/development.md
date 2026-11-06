# 开发与升级

## 本地验证

在仓库根目录运行：

```bash
uv run --project ubuntu python scripts/check.py
```

脚本依次以 `uv sync --locked` 验证独立环境，执行 Ubuntu SDK/桥接/管理器测试、Hermes 插件测试、跨包发布测试和快照 hash 检查。全部是本机离线逻辑测试，不需要 GPU、SSH 或运行中的 ComfyUI。首次重建 uv 环境可能需要下载 Python 或构建依赖。

单独验证两包：

```bash
uv run --project ubuntu python ubuntu/scripts/check.py
uv run --project hermes python hermes/scripts/check.py
```

真实生成属于显式验收步骤，请使用几何示例，并确认现场 ComfyUI/GPU 正常。当前打包验证不会擅自执行真实生成、唤醒、关机或重启。

## 代码位置

| 修改目标 | 位置 |
| --- | --- |
| Hermes 工具定义、任务与投递 | `hermes/plugins/comfy-workflows/` |
| 客户端 SSH / LAN / WoL | `hermes/plugins/ubuntu-runtime/` |
| SDK 编译、HTTP、存储与清理 | `ubuntu/src/comfy_workflow_api/` |
| SSH 服务端与管理 worker | 同目录下的 `workflow_bridge.py`、`runtime_bridge.py`、`runtime_operation_worker.py` |
| 受管 ComfyUI 服务 | `ubuntu/scripts/comfyui-service.py` |
| 当前快照与允许拓扑 | `ubuntu/workflows/` |

## 发布步骤

1. 修改对应包和测试，保持 Python 3.12 与零运行时第三方依赖。不要把 ComfyUI 的环境合并到本模块。
2. 更新 SDK/插件版本和必要文档；协议语义变更时同步 `contract_version` 与校验逻辑。
3. 导出客户端 manifest：

   ```bash
   uv run --project hermes python hermes/scripts/export_contract.py --output ubuntu/config/client-contract.json
   ```

4. 运行根检查脚本与 `git diff --check`；确认不存在用户实际配置、图片、任务、模型或密钥。
5. 记录验证与限制，提交两个包与 manifest。部署前等待已有任务结束，先更新 Ubuntu，再成对安装两个插件并重新加载网关。

工作流修改通过显式同步产生新快照；同步前先从 ComfyUI 保存工作流，再在测试环境校验新拓扑与采样通路。分发前去掉用户默认提示词和参考图名，并更新内容 hash。更换拓扑不能只复制旧 allowlist。

## 数据迁移

本次整理不迁移旧图片或历史任务。若后续需要迁移，应单独备份数据与实际配置，保持任务 ID、服务端文件命名空间及权限一致；运行中的 worker 和防休眠 lease 不能当成可直接迁移的静态文件。不要把运行态数据作为 Git 版本升级的一部分。
