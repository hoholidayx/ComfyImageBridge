# 来源与许可说明

ComfyImageBridge 整理 Hermes 插件与 ComfyUI 工作流调用代码，分离客户端和 Ubuntu 服务端。它不是 Hermes、ComfyUI、Qwen 或 ROCm 的官方发行版。

- Hermes 核心、ComfyUI 核心、自定义节点与模型权重不包含在此仓库；它们各自的许可仍适用。
- `ubuntu/workflows/` 保存用于本地调用的工作流 JSON 配置与来源信息，不包含模型参数。快照中的默认提示词与参考图文件名已替换为公开的几何示例。
- `ubuntu/examples/robot.png` 与 `palette.png` 是几何测试素材；不包含此前用户人物图片、历史生成结果或聊天记录。
- 本仓库未附加新的开源许可授权。使用、再分发或引入第三方项目时，请核对相应权利与许可；不要据此推定为 MIT 等许可。

上游项目参考：[Hermes Agent](https://github.com/NousResearch/hermes-agent)、[ComfyUI](https://github.com/Comfy-Org/ComfyUI)。工作流参数参考的链接见 `ubuntu/workflows/reference/comfy-quality.json`。
