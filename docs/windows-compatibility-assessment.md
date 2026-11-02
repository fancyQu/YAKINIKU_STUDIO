# Windows 适配评估

评估日期：2026-11-02。依据当前源码静态审计，未在 Windows 实机安装或运行；本文是开发范围评估，不是已支持 Windows 的声明。

## 结论与推荐范围

业务流程可以保留，主要改动在启动、平台能力、本机推理和外部工具。直播仍是「本机日语识别 → 一次中文翻译」，完整音频仍是「云端识别 → 校对 → 中文翻译」。

建议首版明确支持 Windows 11 x64，识别采用 faster-whisper/CTranslate2，区分 CPU 与 NVIDIA CUDA；直播优先云端翻译，完整音频继续使用现有 MiniMax／文字服务。Mac 保留 MLX。AMD、Intel GPU 和 Windows ARM 不列入首版 GPU 支持承诺。

| 交付档位 | 用户能做什么 | 改动规模与粗略投入 |
| --- | --- | --- |
| 最小兼容版 | 上传音频云端精修；已有 Tiny CPU 路径用于直播识别，配云端翻译 | 1 个 Windows 启动器，约 5–8 个现有生产文件的调整，加模型准备说明和验证；约 2–4 人日 |
| 推荐 Windows 首版 | 上述功能，加 CT2 Small／Turbo 目录、显式下载和加载；CPU／NVIDIA 分支 | 在最小版上扩展 ASR 适配、模型目录和界面；合计约 10–15 个生产文件；约 5–8 人日 |
| 完整本机体验 | 上述功能，加应用内本机 Qwen 翻译、模型选择下载、状态与恢复；完成 Space 跨平台处理 | 另新增／改造约 4–6 个翻译相关模块，并补平台工具；整体约 10–15 人日或更多 |

这些是按现有模块划分的工程估算，不是逐行修改统计；测试、文档、依赖清单另计。各档投入为累计估算，不相加。完整本机档默认对接成熟推理服务，不包括自研推理引擎、签名安装器、自动更新或 AMD／Intel GPU 的广泛兼容。CUDA 版本冲突和硬件验收可能拉长时间。

最小兼容版不承诺 Tiny 的日语质量或任意 CPU 都达到实时速度，也不包含完整 Space 下载验收。若只交付上传音频功能，范围还可缩小。

## 可直接复用的主体

- 原生网页、黑夜模式、项目列表、字幕预览与导出，无需重写 UI 技术栈。
- Edge 标签页音频使用 `getDisplayMedia`、AudioWorklet 和 WebSocket，不依赖 Mac 专用音频驱动（`static/app.js:959`）。Windows 仍需实际选择标签页并勾选分享音频；本评估未验收系统全局声音采集。
- FastAPI/Uvicorn、云端 ASR、文字 API、录制与字幕队列、切段、时间轴和任务恢复的主体流程。
- PyAV 音频解码、WAV 分卷以及 JSON／SRT／VTT 保存，主要补 Windows 环境和文件占用验收（`yakiniku/import_audio.py:46`、`yakiniku/storage.py:79`）。
- Chromium MV3 的 Space 捕获组件没有 Mac 原生桥接依赖，浏览器端原则上可复用，实际回放仍需 Windows 联调。

## 必须处理的七块

| 模块 | 现状 | Windows 所需调整 |
| --- | --- | --- |
| 启动与依赖 | 只有 zsh `.command`，使用 `.venv/bin/python` 和 `open -a` | 新增 `.bat`／PowerShell；使用 `.venv\\Scripts\\python.exe`，重新创建环境，检查 Python、依赖和端口；不安装 MLX 依赖 |
| 平台能力与默认值 | 默认 MLX，界面固定显示 Apple Silicon；无完整硬件能力目录 | 服务端返回 OS、可用后端、CPU／CUDA 设备、模型格式和内存信息；前端据此提供选项，不能静默换成 Tiny |
| 本机日语识别 | MLX Small／Turbo，另有硬编码 CPU 的 Tiny | 推广现有 CTranslate2 适配为独立后端，支持经验证的 Small／Turbo、CPU int8 与 CUDA 配置 |
| 本机中文翻译 | 只有 MLX-LM；Qwen 下载清单是 MLX 4-bit 格式 | Windows 另接 Ollama 或 llama.cpp 等成熟运行时；定义模型、状态、超时／取消、上下文和恢复契约 |
| 模型目录、下载与内存提示 | 下载／配置枚举含 MLX 分支，内存说明为 Apple 统一内存 | 新增 CT2／GGUF 等对应格式清单，延续显式下载、校验和原子安装；普通内存与显存分开显示，不能沿用 Mac 的估算 |
| FFmpeg 与进程管理 | 发布整理已支持显式 `YAKINIKU_FFMPEG` 与 PATH；Windows 取消只杀父进程 | 实测 `ffmpeg.exe`／PATH 解析；取消时终止本次任务创建的进程树，确保临时文件可清理 |
| 文件、编码与验收 | 大多使用通用 Path／临时文件，部分读取依赖默认编码 | 补显式 UTF-8、用户目录配置、中文／空格路径和长录音验收；新增 Windows CI 与硬件实测 |

### 已确认的具体缺口

1. **内存探测会触发 Windows 异常。** `yakiniku/server.py:647` 调用 Windows 没有的 `os.sysconf`，异常处理仅覆盖 `ValueError/OSError`，未覆盖 `AttributeError`；本机翻译目录接口可能返回 500。应改为跨平台探测或可用的空值兜底。
2. **有 NVIDIA 显卡也不会自动使用。** `yakiniku/model_runtime.py:153` 和旧 `local_transcriber.py:31` 都固定 `device="cpu"`、`compute_type="int8"`。安装 CUDA 不能替代代码适配。
3. **只改模型名称不够。** `live_config.py:17` 的后端／模型枚举、`server.py:120` 的接口输入、`model_downloads.py` 的清单与前端 `live-settings.js` 都限制了现有选择，需一起改。
4. **现有 Qwen 权重不能直接搬到 Windows 推理。** `local_translation.py:53` 使用 `mlx.core` 和 Metal；`local_translation_models.py` 下载的是 MLX 量化文件。需另下载选定运行时支持的格式，继续优先采用阿里 ModelScope 国内来源，具体仓库和摘要需重新核验。
5. **恢复校验也绑定模型格式。** `live_pipeline.py:101` 起的校验包含本机绝对路径、版本以及 config/tokenizer/safetensors 摘要。新增 GGUF／Ollama 时需定义相应身份校验；不能只替换生成函数。复制项目后，已生成字幕可以复用，原场次补全不能假定模型环境相同。
6. **FFmpeg 查找已在 macOS 发布整理时补齐一部分。** `space_downloads.py` 支持 `YAKINIKU_FFMPEG`、仓库工具和系统 PATH，移除了对个人 Codex 技能目录的依赖；仍需 Windows 真机验证 `ffmpeg.exe`、路径及进程树取消。
7. **Windows 子进程树和编码要补齐。** `space_downloads.py:388` 的非 POSIX 分支只终止 Python 父进程；`:216`、`:379` 读取 JSON 未指定 UTF-8，中文系统默认编码可能导致恢复失败。

### 无需扩大为重写的问题

- `pathlib.Path`、`tempfile`、先关闭文件再 `os.replace` 的思路可继续使用。
- `chmod(0600)` 在 Windows 不等于 Unix 私有权限，应使用当前用户目录的 ACL；它本身不意味着程序必然启动失败。
- `os.link` 在 NTFS 上可用，当前临时输出同盘；支持 exFAT／特殊共享盘时再提供安全替代发布方式。
- 项目目录使用 UUID，实际文件为固定的 `audio.wav`、`source.mp3` 等名称；直播标题不会直接变成磁盘路径。独立下载 CLI 的自定义文件名需单独补 Windows 保留名／非法字符处理。
- `YAKINIKU_DATA_DIR` 已存在（`server.py:420`），启动器可将用户数据放入 `%LOCALAPPDATA%`。若应用安装在 Program Files，模型目录也应统一迁入用户可写目录；现在的模型根仍在项目 `data/models`（`model_runtime.py:25`）。

## 本机翻译的两条路线

**外部本机服务先接入：** 当前文字 API 地址校验允许 HTTP 回环地址（`config.py:16`），可以评估复用 OpenAI 兼容接口连接用户自行安装的 Ollama／llama.cpp 服务。这样能减少推理集成，但目前仍有连接凭据门禁、云端路由标签、超时和能力展示问题，不能宣称已完整支持。应用内「选模型 → 下载 → 加载」体验不会因此自动具备。

**应用统一管理：** 在已有翻译抽象后新增运行时适配器和跨平台模型清单，统一下载、加载、取消、故障和恢复状态。工作量更大，但能保持 Mac／Windows 相同的操作入口。若选 GGUF，需重新核验国内来源、文件摘要和各硬件内存／显存估算。

## 验收与开发顺序

1. 先确定首版 Windows 版本、CPU-only／NVIDIA 范围，创建干净的 Python 3.12 x64 环境并固定验证过的依赖。
2. 完成 Windows 启动、平台能力与默认模型、内存探测兜底，验证上传音频云端流程。
3. 推广 CT2 ASR，验证 Small／Turbo 的显式下载、离线加载和 CPU／CUDA 行为；保留 Mac MLX。
4. 在 Windows Edge 实测标签页音频、停止尾包、30 分钟录制、中文与空格路径、字幕积压及恢复；速度和日语质量不能由 Mac 的 MLX 结果代替。
5. 若首版包含 Space，补 FFmpeg.exe 查找、真实进程树取消和回放下载验收。
6. 最后加入本机 Qwen 服务／GGUF 管理。ASR 与翻译可能争用显卡，应测试显存峰值、排队与模型驻留策略，不照搬 Apple 统一内存预算。

现有自动化测试有大量可复用部分，但 POSIX 进程组、Unix 权限断言和 symlink 测试需要平台分支。没有 Windows 实测前，不把「Linux/Mac 测试通过」写成 Windows 已支持。
