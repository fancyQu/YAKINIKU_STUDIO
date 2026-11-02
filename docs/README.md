# 开发文档

本目录按当前代码整理，更新日期：2026-11-02。新开发者从技术栈与环境开始；既有实现记录保留当时的验证条件与限制。

## 当前开发入口

| 文档 | 内容 |
| --- | --- |
| [技术栈与开发指南](development.md) | 完整技术栈、依赖版本、项目目录、新机器安装、模型准备、配置、测试及开发约定 |
| [后端架构](backend-architecture.md) | FastAPI 接口、直播与文件处理链路、模型执行、持久化、恢复及配置隔离 |
| [前端与浏览器组件](frontend-and-extension.md) | 页面模块、AudioWorklet、WebSocket、字幕显示、MV3 扩展、HLS 下载与打包 |
| [GitHub 开源发布检查](open-source-checklist.md) | 提交范围、敏感信息、第三方许可、待办事项与发布步骤 |
| [macOS 发布验证](macos-release-validation.md) | 源码预览的安装、自动化测试、离线模型检查与已知限制 |
| [Windows 适配评估](windows-compatibility-assessment.md) | 桌面跨平台的复用范围、本机推理替换、平台缺口与工作量估计 |
| [Android 实时识别与保存评估](android-live-asr-assessment.md) | 手机端收窄后的业务范围、原生采音、本机识别候选、保存恢复与工作量估计 |

## 功能说明与历史记录

| 文档 | 阅读用途 |
| --- | --- |
| [项目 README](../README.md) | 用户操作、功能、隐私与常见问题 |
| [直播本机中文翻译](live-local-translation.md) | Qwen 模型操作、资源估计与验证边界 |
| [Space 回放音频下载](space-audio-download.md) | 浏览器组件的加载、使用与限制 |
| [直播录音精准校验](live-refinement.md) | 完整录音的独立精修流程 |
| [页面导航与按钮反馈](ui-navigation-review.md) | 页面导航设计与验收记录 |
| [MLX 本机准备记录](mlx-local-setup.md) | 历史环境、Small 模型来源与短样本数据 |
| [MLX 直播开发交接](live-mlx-development-brief.md) | 当时的开发需求与验收目标 |
| [MLX 直播实现记录](live-mlx-implementation.md) | 接入阶段的实现与回归记录 |
| [ASR 适配器验证](live-asr-runtime-validation.md) | 独立适配器测速方法及其适用范围 |
| [API 配置隔离修复](api-config-isolation-fix.md) | 配置保存问题的修复与历史验收 |

历史记录中的测试数量、端口、模型状态和“尚未接入”等描述只对应记录当时。复现当前开发环境以 [development.md](development.md) 和依赖文件为入口；`data/`、`artifacts/` 中的本机报告、截图和录音不随公开仓库提供。
