# 变更记录

## 0.1.0-preview.1 — 2026-11-02

首个 macOS 源码预览版本，面向 Apple Silicon 和 Microsoft Edge。

- 直播：本机日语识别后执行一次中文翻译；支持云端文字服务或 MLX-LM 本机 Qwen。
- 文件：完整音频云端识别、日语校对、中文翻译及独立录音精修。
- 录音保存、字幕时间轴、队列积压与停止后补全、回放及 SRT/VTT/TXT 导出。
- 显式模型下载与校验；Qwen 翻译权重使用阿里 ModelScope 国内源。
- Space 回放下载及配套浏览器组件；FFmpeg 由用户另行安装。
- 浅色／深色界面，深色模式使用中性灰背景。
- macOS 环境安装入口、依赖约束、GPL-3.0-only 许可与 GitHub Actions 检查。

这是源码预览，不包含已签名／公证的 `.app` 或 `.dmg`。Windows、Android、真实直播长时间稳定性、Small/Turbo 对比和本机 Qwen 质量／峰值内存仍需后续验证。
