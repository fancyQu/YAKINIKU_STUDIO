# GitHub 开源发布检查

更新日期：2026-11-02。目标仓库：[fancyQu/YAKINIKU_STUDIO](https://github.com/fancyQu/YAKINIKU_STUDIO)。本次交付为 macOS 源码预览版；实际发布记录以仓库提交及 Actions 结果为准。

## 1. 已完成的整理

- [x] 提供 [开发文档索引](README.md)、[技术栈与开发指南](development.md)、[后端架构](backend-architecture.md) 和 [前端与浏览器组件](frontend-and-extension.md)。
- [x] README 增加开发入口，安装示例改为项目根目录执行。
- [x] Git 忽略本机数据、虚拟环境、环境密钥文件、下载素材、验收截图/报告、日志和生成的浏览器 ZIP。
- [x] 保留扩展的第三方 `LICENSE` 与来源 `NOTICE.md`；保留 FFmpeg 来源说明。
- [x] 核验主 Python 与 Node 回归测试命令，记录真实结果及其验证边界。

## 2. 本次源码预览的发布准备

- [x] **许可证。** 维护者明确选择 GPL-3.0-only；已添加根 `LICENSE`、第三方声明和贡献说明，保留浏览器组件原许可及 Cat Catch 来源。
- [x] **候选源码检查。** 排除个人设置、录音、字幕、模型、验收报告、FFmpeg 和生成 ZIP。文本模式扫描的命中项已定位到测试占位凭据及模拟媒体地址，未确认实际密钥；这不等于保证扫描能发现一切问题。
- [x] **干净环境安装。** 在同一台 Apple Silicon Mac 的全新 Python 3.12 环境安装完整依赖，并通过 435 项后端测试。纯源码副本使用空数据目录启动，检查页面和模型目录接口；没有开始录制或隐式下载。新物理机器及实际浏览器音频共享仍需独立验收。
- [x] **Space 工具准备。** 网页后端已支持 `YAKINIKU_FFMPEG`、仓库工具及系统 PATH；FFmpeg 不随源码分发。安装脚本生成含许可证的浏览器组件 ZIP，扩展仍由用户手动加载。7 项合成 HLS 集成测试通过，真实 X 回放未由此获得验证。
- [x] **发布素材范围。** 不提交实际录音、字幕、配置页截图、个人缓存、签名媒体链接或捕获密钥；本次不提供真实素材演示附件。
- [x] **发布描述。** README 明确这是 Apple Silicon 的 macOS 源码预览，Windows／Android 是评估文档；真实直播时延、长时稳定性、日语准确率和 Qwen 性能仍需单独验收。

## 3. 提交范围

| 路径 | 公开策略 |
| --- | --- |
| `yakiniku/`、`static/` 源码、`tests/`、`scripts/` | 可以提交，先检查硬编码地址、凭据和测试素材 |
| `requirements*.txt`、macOS 约束、`run.py`、安装／启动脚本、文档和 `.github/` | 可以提交；文档示例使用通用路径和占位内容 |
| `browser-extension/space-capture/` | 随源码提交，并完整保留 GPL 许可证与改编来源 |
| `tools/hls-audio-download/` | 提交脚本、测试和来源说明；FFmpeg 二进制排除 |
| `data/`、`.venv/`、`.venv-mlx/`、`.env`、`.env.*` | 已忽略，不公开；未来 `.env.example` 仅可含占位值 |
| `downloads/` | 已忽略，含本机回放素材和带查询参数的媒体清单 |
| `artifacts/` | 已忽略，含本机验收截图、实际字幕、路径和报告 |
| `static/space-capture.zip` | 已忽略；由白名单源码重新打包 |
| `tools/hls-audio-download/bin/ffmpeg` | 子目录规则已忽略；不跟随源码上传 |

模型权重默认在已忽略的 `data/models/`，由用户显式下载。模型权重的许可、来源与使用限制独立于应用源码；如果未来镜像或重新分发模型，应逐个核对上游模型卡和许可证。

FFmpeg 当前来源说明指向 imageio-ffmpeg 0.6.0 的 macOS arm64 构建，并声明 GPL-2.0-or-later。`imageio-ffmpeg-LICENSE.txt` 不等于 FFmpeg 构建的完整许可证和源码履约材料。若未来发布包含 FFmpeg 的安装包，需另外准备对应构建、许可文本与源码提供方式；当前文档只指导用户本地安装。

## 4. 提交前的本机复核命令

以下命令在项目根目录执行，不会自动提交或发布：

```sh
git status --short
git ls-files --others --exclude-standard
git status --short --ignored
git check-ignore -v data/settings.json artifacts/local-check.json downloads/example.mp3 \
  static/space-capture.zip tools/hls-audio-download/bin/ffmpeg .env .env.local

.venv-mlx/bin/python -m pip check
.venv-mlx/bin/python -m unittest discover -s tests -v
node --test tests/*.test.cjs
```

敏感信息检查应覆盖候选源码、配置、截图与发布附件。本轮候选文本模式扫描没有确认真实 API 密钥，不能据此断言不存在泄漏；截图未逐图审核，已整体排除。运行密钥扫描工具时不要把完整命中内容直接粘贴到公开日志。

确认许可证和文件范围后，可以显式暂存源码与文档，再进行最终审阅：

```sh
git add .gitignore README.md LICENSE THIRD_PARTY_NOTICES.md CONTRIBUTING.md CHANGELOG.md \
  requirements.txt requirements-mlx.txt constraints-macos-py312.txt run.py \
  "安装macOS环境.command" "启动烤肉台.command" .github yakiniku static tests scripts docs browser-extension tools
git diff --cached --stat
git diff --cached --check
git diff --cached
```

最终审阅后创建首次提交并推送至维护者明确指定的仓库。不要覆盖远端已有提交或使用强制推送。

## 5. 建议在首个正式版本前补齐

- [x] 整理直接 Python 依赖及 macOS/Python 3.12 版本约束，不将其当跨平台锁文件。
- [x] 增加 GitHub Actions：基础后端、Node、macOS arm64 安装与媒体检查；真实 Metal 推理需在匹配 Apple 硬件上单独验证。
- [x] 添加贡献说明及版本更新记录。
- [ ] 按维护需求补充问题模板及安全问题反馈渠道。
- [ ] 记录经过测试的系统、Python、浏览器版本，以及模型和 FFmpeg 的最低运行要求。
- [ ] 以合成或授权素材补齐实际直播、长时录制、恢复、字幕准确率和本机翻译的验收记录。

已完成上述源码预览准备；完整的真实直播稳定性和模型质量验收尚未完成，不将此版本描述为已完成全部功能验收的稳定版。
