# Ubuntu 硬件监控与睡眠恢复

本功能按用户明确要求增加系统托管。监控每 15 秒采样 CPU 与 AMD GPU；有任务时阻止睡眠，空闲时先停止 ComfyUI，唤醒后仅恢复睡前正在运行的实例。ComfyUI 仍使用原 `run_comfyui.sh`，不调整 HSA、MIOpen、uv 或 ROCm 依赖。

## 安装与管理

先部署完整 Ubuntu 包并运行 `uv sync --locked`，保留已有 `config/settings.toml` 与 `data/`。将示例配置中的 `[monitor]`、`[monitor.smtp]` 合并到实际配置。安装使用当前登录的 ComfyUI 用户；从 root 登录环境执行时须传 `--user`。

```bash
cd ~/workspace/comfy-workflow-api
sudo bash scripts/hardware-monitor.sh install
bash scripts/hardware-monitor.sh status
bash scripts/hardware-monitor.sh check
```

安装器检查部署路径、账号和已有单元归属，先在临时目录验证单元，再安装 root 所有的协调器副本并启用服务。升级会重启监控，核对新 PID 和新鲜健康快照；安装或卸载失败会恢复原有受管文件、权限、启用状态和监控运行状态，回滚失败则明确报告。该回滚不包含已由部署工具替换的 SDK 源码，源码仍须使用部署备份恢复。系统协调器只执行固定的服务操作，所有 SDK 代码均以 ComfyUI 用户运行。监控独立于 SSH 和工具终端运行；stdin 关闭，stdout/stderr 写入 journal。

四个单元：`myhermes2comfy-monitor.service` 常驻采样和邮件投递；`myhermes2comfy-sleep.service` 在 sleep.target 之前确认停机；`myhermes2comfy-resume.service` 协调恢复与门禁释放；`myhermes2comfy-resume-owner.service` 承载恢复启动的 ComfyUI 进程。后两个单元不配置开机启动。

```bash
sudo bash scripts/hardware-monitor.sh stop
sudo bash scripts/hardware-monitor.sh start
sudo bash scripts/hardware-monitor.sh restart
journalctl -u myhermes2comfy-monitor.service -n 100 --no-pager
journalctl -u myhermes2comfy-sleep.service -u myhermes2comfy-resume.service --no-pager
```

`start/stop/restart` 只控制监控。ComfyUI 继续使用 `scripts/comfyui-service.sh` 管理；不要通过直接 stop 恢复承载单元来退出 ComfyUI。卸载前等待任务完成、正常停止由恢复单元承载的 ComfyUI，并结束未完成的睡眠周期：

```bash
sudo bash scripts/hardware-monitor.sh uninstall
```

卸载保留运行数据和邮件凭据；恢复 cgroup 仍有进程时拒绝操作。

## 邮件配置

首次在 Ubuntu 创建私密配置；已有文件应直接编辑，避免覆盖凭据：

```bash
mkdir -p ~/.config/myhermes2comfy
install -m 600 config/smtp.example.toml ~/.config/myhermes2comfy/smtp.toml
```

填写 SMTP 主机、端口、用户名、邮箱应用密码、发件人和收件人。默认 587 + STARTTLS，也支持 `security = "ssl"` 配合 465。系统验证 TLS 证书。配置路径通过 `[monitor.smtp].secret_file` 指定；凭据文件须是当前用户所有、权限 0600 的普通文件。私密配置不得加入仓库。

```bash
bash scripts/hardware-monitor.sh test-email
bash scripts/hardware-monitor.sh status
```

未配置时显示 `notifications_configured=false`。测试邮件和故障通知进入持久队列，由独立 worker 发送；连接超时 5 秒，单次总预算 15 秒，失败按 1、5、15、30、60 分钟退避。投递为至少一次语义，使用稳定 Message-ID；状态显示积压和最近投递错误。网络恢复后补发并保留原始发生时间。

## 报警与状态

GPU 以 PCI 地址识别，避免 DRM card 编号和 ROCm 编号变化。GPU 查询在独立进程内完成，5 秒超时；查询与子进程持有同一探测锁，残留进程未退出时不会反复创建查询。监控状态和 Hermes 的 GPU 状态都读取缓存，保留原采样时间与过期标记，不会直接访问 GPU sysfs；`check` 执行一次受同一锁保护的采样。功率和运行电源状态也由隔离探测进程采集。

- 关键读数连续三次不可用时报警；可选字段的 N/A 不参与故障判断，未知值不写成 0。
- 新增恢复失败、GPU fault、驱动超时等严重内核事件立即报警。当前 boot 的游标持久化，重启监控不会重复发送旧错误。
- 温度距硬件 crit 值 10°C 持续报警，距 crit 值 5°C 立即报警；无 crit 的通道保持未知阈值。高 CPU/GPU 利用率本身不报警。
- 持续故障每 30 分钟提醒，连续三次恢复且近期无新增严重错误后通知监测恢复。监测恢复与真实推理验证分别报告。

配置项位于 `config/settings.toml` 的 `[monitor]`。状态、告警和邮件队列在 `data/hardware-monitor/`；探测锁和进程身份在 `data/hardware-probe/`；睡眠周期在 `data/sleep-cycle/`。目录相对于实际 `data_dir`。

## 睡眠与恢复

前置服务建立门禁后检查提交中的请求、活动 lease、ComfyUI 队列和监听归属。忙碌、未知或归属不明时拒绝睡眠；空闲时复用原管理器，SIGINT 最多等待 120 秒，再依次 TERM/KILL 各 5 秒。每步复核进程身份；外来端口和复用 PID 不会被结束。若信号已发出后又出现新任务，会保留失败门禁，不把部分退出误当成可以重新接收任务。准备阶段总上限 180 秒。

唤醒后 GPU 最多等待 120 秒取得三次有效采样，再使用原脚本启动并做真实 64×64 fast 烟测。独立协调器确认恢复单元已完成激活、当前实例 ready 后开放新请求。每周期只自动尝试一次；失败保留门禁、记录原因并报警。

```bash
# 修复现场原因后，显式重试失败恢复；不会重载驱动或重启整机。
sudo bash scripts/hardware-monitor.sh retry
# 未停机则解除准备；已停机则安排恢复；退出不确定时保持保护。
sudo bash scripts/hardware-monitor.sh abort
```

门禁覆盖本项目 SDK、Hermes 和管理入口；绕过这些入口向 ComfyUI 提交的请求仍由退出前的队列复核保护。后台管理操作记录派发时的 boot 与睡眠周期，并在取得 lease、实际操作前再次核对，迟到的旧操作不能作用于唤醒后的新实例。恢复执行中再次请求睡眠会被拒绝。整机重启后，旧 boot 的周期不会继续阻塞新请求。

睡眠失败时：未停止实例则解除本次门禁，已确认停机则安排恢复，退出不确定则保持保护。内核不可中断的 GPU 等待无法由用户态脚本修复；监控限制残留探测数量并报告证据。整机卡死或无网络时，本机无法即时发送邮件；网络恢复后可补发已入队事件。

## 验收与回滚

先运行仓库根 `scripts/check.py`，再检查安装单元和独立连接下的 MainPID、进程启动标识、端口与监控快照。真实验收应在无活动任务时进行两次睡眠／唤醒，确认睡前退出、醒后 PID 更新和真实出图；睡前手动关闭的实例应保持关闭。

配套脚本默认只显示计划；明确加 `--run` 后以 sudo 执行两轮真实睡眠，分别验证 ComfyUI 停机和运行两种情况，并恢复原始开关状态。需要外部 Mac 在每轮确认 Ubuntu 离线 15 秒后通过已有 WoL 配置唤醒；脚本不会自行重启机器或重载驱动。

```bash
python3 scripts/verify-hardware-sleep.py
sudo /usr/bin/python3 scripts/verify-hardware-sleep.py --run
```

进度与最终收据保存在实际 `data_dir/hardware-monitor/sleep-verification.json`，含每轮 token、睡眠时钟差、前后实例及恢复结果。验收脚本会刷新已安装协调器、重启并核验监控；活动任务或不明归属会中止验收。远程 SSH 发起睡眠可能需要系统认证，因此应由操作者从有 sudo 权限的终端启动脚本。

升级前备份 Ubuntu 源码、实际配置和客户端插件，运行数据单独保留。两端成套升级并检查 `stale_copy`；不要用默认配置覆盖实际配置。回滚时先按上述流程卸载本功能，再恢复成套源码与客户端版本、执行锁文件同步；保留 `data/`，不覆盖 ComfyUI 原脚本或环境。部署验收记录应区分离线测试、已安装服务、邮件配置和真实睡眠验证。
