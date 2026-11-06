# Ubuntu Runtime 0.3.1

安装和连接配置见[Hermes 包说明](../../README.md)。此目录只包含Hermes客户端；服务端实现位于独立Ubuntu包的comfy_workflow_api.runtime_bridge与runtime_operation_worker，不再复制一整套插件源码到Ubuntu。

按需唤醒固定 Ubuntu，并通过原 run_comfyui.sh 管理 ComfyUI。不修改 Python/ROCm 依赖、模型、采样参数或电源策略，没有新增常驻服务。

## 调用

1. comfy_service(action=ensure_ready)：唯一允许 WoL 的动作，最多180秒等待SSH。只在 ready=true 后提交生图。
2. 返回 in_progress 时，用 action=status 和原 operation_id 续查。单次工具等待最多30秒，后台操作独立继续；不要重复 restart。
3. 正常使用comfy_image即可在内部完成准备和提交；兼容的comfy_generate / comfy_edit需先准备服务，再用comfy_task查询或定向取消。

power_state=online 表示主机已经醒来，ComfyUI未就绪不等于“唤醒还需要批准”。status 全程只读，不唤醒、不烟测、不拿抑制锁。

## 退出与恢复：不需要手动确认

用户授权已更新：受管进程收到退出信号后超时，可以自动强制退出。已移除 /comfy-recover 和 confirm-recovery 入口，不再生成恢复确认请求。旧 request_id 仅供历史审计，不需输入。

- 正常 stop/restart 保护活动队列。跨休眠的旧实例在空闲时自动正常重启。
- SIGINT只发到实际Python进程一次，不同时发给会转发信号的uv父进程。正常退出最多等待120秒，让ROCm清理完成；期间service.phase=stopping，操作仍是in_progress。
- 超时自动 SIGTERM 等5秒，再 SIGKILL 等5秒；每一步复核 PID、启动标识、用户、启动组和监听归属。外部进程、身份变化或新任务出现时拒绝误杀。
- recover 对异常受管实例执行一次恢复，可在SIGINT10秒后自动升级；可能打断该异常实例的任务，不用于普通健康忙碌队列。最多重建一次服务并烟测，失败不循环、不重启主机、不自动重提原任务。
- 已发SIGINT的实例继续等原截止时间，不重复发送。进程已退出则确认退出证据并正常启动，旧确认不会阻塞。

就绪要求真实64×64 fast/6步烟测：热30秒、冷120秒，新种子且采样不走缓存。输出单独保留最近10次。HTTP与端口只表示存活。执行错误/可靠停滞证据使旧健康证明失效。

后台操作最长600秒，独立会话、stdin=/dev/null、日志重定向；记录PID、启动标识和命令。防睡眠锁在启动、排队/运行期间存在，无租约/任务后10秒释放。

## 收据

Mac：plugin-data/ubuntu-runtime/operations/<id>/receipt.json。
Ubuntu：data/runtime-operations/<id>/；旧进程退出和每步信号在data/comfyui-service/retired-instances/。
版本、加载哈希与Ubuntu的config/client-contract.json对比，stale_copy或缺少契约时拒绝修改。更新后开启Hermes新会话使用新定义；工具参数仍为action、operation_id、wait_seconds，不增加force权限参数。

```bash
hermes ubuntu-runtime comfy_service --args '{"action":"ensure_ready"}'
hermes ubuntu-runtime comfy_service --args '{"action":"status","operation_id":"UUID"}'
hermes ubuntu-runtime comfy_service --args '{"action":"recover"}'
```
