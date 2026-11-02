# macOS 源码预览验证

日期：2026-11-02。版本说明见 [CHANGELOG](../CHANGELOG.md)。目标仓库为 [fancyQu/YAKINIKU_STUDIO](https://github.com/fancyQu/YAKINIKU_STUDIO)。

## 环境与安装

- 本机系统：macOS 15.8.1，Apple Silicon / arm64，Python 3.12.14，Node.js 24.21.0。
- 通过独立新建的 Python 3.12 虚拟环境安装 `requirements-mlx.txt` 和 `constraints-macos-py312.txt`；`pip check` 通过。
- 显式声明应用直接使用的 Pydantic、Starlette、NumPy、PyAV；macOS 约束固定本轮安装组合，不承诺其他平台或 Python 版本。
- 新增 `安装macOS环境.command`，校验架构与 Python 版本、安装依赖、检查一致性并生成浏览器组件包；不下载模型。
- 启动脚本检查 Edge 和已有环境，不再用 macOS 自带旧 Python 自动创建运行环境，并等待服务就绪后打开工作台。

## 检查结果

| 检查 | 结果与范围 |
| --- | --- |
| 新环境 Python 回归 | 435 项通过，使用模拟服务、假权重和临时目录，不调用收费 API |
| Node 前端回归 | 30 项通过，无前端构建依赖 |
| HLS 媒体集成 | 7 项通过，使用合成音频、临时本机 HTTP 服务及本机 FFmpeg |
| 纯源码启动 | 复制候选源码到临时目录，不带模型、原设置或录音；使用新环境成功启动 |
| 首次访问 | 工作台及功能页面、服务健康、直播设置、ASR 状态、翻译模型目录等接口返回 200；无需已有用户数据 |
| 隐式操作检查 | 读取页面没有开始录制或下载模型；跨来源配置请求被拒绝 |
| 本机 MLX 检查 | 已安装 Small 模型完成 Metal 加载和一次热运行；输入为合成静音，不证明识别准确率或真实直播速度 |
| 启动与工作流 | 两个 zsh 入口语法通过，Actions YAML 可解析；云端 Actions 运行结果以仓库记录为准 |

Space 网页下载支持 `YAKINIKU_FFMPEG` 和 PATH，避免依赖个人 Codex 目录。浏览器组件包仅从固定源码白名单生成，包含原 GPL 许可及来源说明，生成物不提交 Git。

## 发布内容

主代码采用 GPL-3.0-only，保留第三方声明。提交源码、测试、依赖入口、版本约束、安装／启动脚本、开发文档和 CI。API 密钥、个人配置、录音、字幕、模型权重、FFmpeg 二进制、临时报告和验收截图不随源码上传。

## 验证边界

这是一台现有 Mac 上的干净虚拟环境和纯源码目录检查，不是第二台物理 Mac 的安装验收。尚未完成本轮实际直播共享、长时间稳定性、真实 API 延迟、日语准确率、Small/Turbo 对比、本机 Qwen 质量和峰值内存验收。

当前仅提供源码运行，不包含签名、公证、自动更新或 `.app`／`.dmg`。Windows 和 Android 文档属于方案评估，不是已发布客户端。详细功能边界见 [README](../README.md) 和 [开发指南](development.md)。

原始日志和机器相关报告保存在已忽略的本机 `artifacts/release-macos/`，不作为公开仓库的必需文件。
