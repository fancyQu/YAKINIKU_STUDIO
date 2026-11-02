# 参与开发

当前运行目标是 Apple Silicon Mac、ARM64 Python 3.12 和 Microsoft Edge。开发环境见 [开发指南](docs/development.md)。Windows 与 Android 文档是适配评估，不代表已有可运行客户端。

提交改动前请执行：

```sh
.venv-mlx/bin/python -m pip check
.venv-mlx/bin/python -m unittest discover -s tests -v
node --test tests/*.test.cjs
```

改动 HLS 下载时另运行 `tools/hls-audio-download/tests`；需要 FFmpeg、OpenSSL 和本机临时端口。基础自动化测试使用模拟服务，不应调用收费 API 或下载真实模型。

请保持直播和文件精修的模型、配置与执行流程独立。录音保存不能依赖识别或翻译成功；新增模型仍需显式下载和完整性校验。不要自动更换用户保存的模型、连接或下载来源。

不要提交 API 密钥、个人配置、录音、字幕、模型权重、带签名的媒体链接或 FFmpeg 二进制。问题复现使用合成音频和占位凭据。浏览器共享、GPU 性能或真实模型质量的结果，请注明设备、条件与验证范围。

提交代码即表示你有权以本项目的 GPL-3.0-only 许可贡献该代码；引入第三方代码时保留原有许可证和来源声明。
