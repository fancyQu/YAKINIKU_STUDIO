# 第三方组件与分发范围

YAKINIKU STUDIO 主代码采用 **GPL-3.0-only**，许可全文见根目录 [LICENSE](LICENSE)。依赖、模型和第三方工具保留各自的许可证与使用条件。

## Space 浏览器组件

`browser-extension/space-capture/` 的 fetch/XHR 检查方式改编自 xifangczy 的 [Cat Catch](https://github.com/xifangczy/cat-catch)，采用 GPL-3.0-only。上游提交、修改范围和来源见该目录的 [NOTICE.md](browser-extension/space-capture/NOTICE.md)，该目录的 [LICENSE](browser-extension/space-capture/LICENSE) 及源码 SPDX 标识一并保留。打包组件时同样包含许可证和来源说明。

## Python 与推理依赖

FastAPI、Uvicorn、HTTPX、PyAV、faster-whisper/CTranslate2、MLX、mlx-whisper、mlx-lm 等由用户通过依赖清单安装，不随本仓库源码附带其二进制或源码。各组件以所安装版本的上游许可证为准；主代码许可证不改变第三方组件的权利。

## 模型

仓库不包含 Whisper 或 Qwen 权重。模型仅在用户明确点击下载后获取；来源、版本和摘要见模型清单及下载界面。Whisper、各个 Qwen 版本与其转换／量化版本的许可和使用限制由相应上游文件规定，不因应用采用 GPL 而改变。需要重新分发模型时应逐项核验。

## FFmpeg

仓库不分发 FFmpeg 可执行文件。用户自行安装的 FFmpeg 构建适用其自身许可证。`tools/hls-audio-download/bin/FFmpeg-NOTICE.txt` 和 `imageio-ffmpeg-LICENSE.txt` 记录历史本机工具的来源；它们不代表仓库已提供某个 FFmpeg 构建的完整源码履约材料。未来若制作附带 FFmpeg 的安装包，应针对实际构建另行准备许可证及对应源码提供方式。

## 配置页设计参考

服务商配置交互参考 Cherry Studio、Open WebUI 和 LobeHub 的公开设计，具体链接见 [README](README.md)。这些引用不将上游项目的名称、商标或许可证重新授予本项目。
