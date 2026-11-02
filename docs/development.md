# 技术栈与开发指南

更新日期：2026-11-02。本文根据仓库代码与依赖入口整理，用于新机器搭建、功能开发和 GitHub 维护。当前为 macOS 源码预览版。

## 1. 项目定位与运行边界

烤肉台（Python 包名 `yakiniku`）是运行在本机的日语音频字幕工作台。主要能力包括标签页音频录制、日语识别、简体中文翻译、完整音频导入、录音精修、字幕预览与导出，以及 X Space 已结束回放的音频下载。

当前主要运行环境为 **macOS + Apple Silicon + ARM64 Python + Microsoft Edge**。MLX 路径依赖 Apple Metal GPU；Tiny 是显式选择的 CPU 兼容识别路径。Windows、Linux、Intel Mac、Safari、Firefox 及其他浏览器的完整功能尚未完成兼容验收。

入口 `run.py` 使用 Uvicorn 启动 FastAPI，固定监听 `127.0.0.1`，默认端口 `8765`。这是单机、单进程应用：活动会话、模型实例、队列与任务锁均在进程内管理。开发和使用时保持一个服务进程；多 worker、多人公网部署需要另行设计。

## 2. 技术栈总览

| 层级 | 技术 | 在项目中的用途与入口 |
| --- | --- | --- |
| 前端语言与布局 | HTML5、CSS3、原生 JavaScript、ES Modules | `static/` 多页面界面；无前端编译步骤 |
| 浏览器音频 | `getDisplayMedia`、Web Audio API、AudioWorklet | 标签页采集、PCM 编码、识别副本重采样；`app.js`、`audio-worklet.js` |
| 浏览器通信 | Fetch、WebSocket、BroadcastChannel | HTTP 配置/项目接口、音频二进制与状态消息、独立字幕窗口同步 |
| 后端语言 | Python，建议统一使用 3.12 ARM64 | Web 服务、异步任务、音频处理、模型与文件管理 |
| Web 服务 | FastAPI、Uvicorn、Starlette、Pydantic v2 | HTTP/WebSocket、ASGI 生命周期、静态资源、请求校验与中间件 |
| 异步与并发 | `asyncio`、`ThreadPoolExecutor`、线程锁 | 有界队列、本机推理串行化、云端网络请求并发与后台任务 |
| HTTP/API 客户端 | HTTPX | 云端识别与文字请求、SSE 响应解析、模型列表及权重下载 |
| 旧语音协议适配 | `websockets` | 保留的云端 Realtime 转写客户端；普通新直播使用本机 ASR |
| 主力本机 ASR | `mlx-whisper`、MLX、Metal | Whisper Small / Large v3 Turbo 多语言权重的日语识别 |
| CPU 兼容 ASR | `faster-whisper`、CTranslate2 | Tiny 模型 CPU int8 推理；需显式选择并准备本机权重 |
| 本机中文翻译 | `mlx-lm`、MLX、Qwen 4-bit | Qwen3 4B Instruct 2507，Qwen2.5 1.5B / 3B / 7B 候选 |
| 云端文字协议 | OpenAI Chat Completions、OpenAI Responses、Anthropic Messages | 手写 HTTP 适配，连接与模型分别配置；未依赖 OpenAI/Anthropic SDK |
| 云端文件识别 | OpenAI 兼容 `/audio/transcriptions`、MiniMax 原生 `/speech_to_text` | 完整音频导入和录音精修；具体能力由所选服务决定 |
| 数值与重采样 | NumPy、SciPy `resample_poly` | PCM/float32 转换及采样率转换；Tiny 无 SciPy 时有线性插值分支 |
| 音频文件解码 | PyAV、其使用的 FFmpeg 库 | 完整音频解码、48 kHz 双声道预览、16 kHz 单声道识别分块 |
| HLS 下载与转码 | Python `urllib`、线程池、FFmpeg 命令行 | M3U8 资源下载、普通 AES-128 HLS、本地 MP3 转码与完整解码验证 |
| 浏览器组件 | Chromium Extension Manifest V3 | X Space 指定任务的 fetch/XHR 捕获、service worker、本机桥接 |
| 模型分发 | Hugging Face、ModelScope、固定文件清单与 SHA256 | ASR 从 Hugging Face、Qwen 翻译权重从 ModelScope 显式下载 |
| 数据持久化 | JSON、文件系统、Python `wave` | 原子保存设置和会话、PCM16 WAV 分卷、恢复检查点 |
| 字幕格式 | SRT、WebVTT、TXT、JSON | 日语、中文、双语字幕及可再次处理的会话数据 |
| Python 测试 | `unittest`、`IsolatedAsyncioTestCase`、FastAPI TestClient、HTTPX MockTransport | 后端、恢复、协议与配置回归 |
| JavaScript 测试 | Node.js 内置 test runner、`assert`、`vm` | 浏览器 API 替身、界面逻辑与 AudioWorklet 测试 |
| 启动与打包 | zsh、Python `zipfile` | macOS 双击入口、浏览器组件白名单 ZIP 打包 |
| 版本控制与检查 | Git、GitHub Actions | Linux 基础回归、Node 回归与 macOS arm64 安装／媒体检查；没有自动构建签名应用 |

仓库没有 `package.json`、npm 依赖、React/Vue、TypeScript、Vite/Webpack、数据库、Redis 或 Celery。Node.js 只用于开发测试，启动应用无需 Node.js。模型库自身引入的 Transformers、tokenizers、safetensors、Hugging Face Hub 等属于推理依赖链，不是应用层的服务框架。

## 3. Python 依赖与版本策略

直接安装入口：

| 文件 | 声明 |
| --- | --- |
| `requirements.txt` | FastAPI、Uvicorn、HTTPX、websockets、faster-whisper，以及直接导入的 Pydantic、Starlette、NumPy、PyAV |
| `requirements-mlx.txt` | 包含上述基础依赖，固定 `mlx-whisper==0.4.3`、`mlx==0.32.4`、`mlx-lm==0.32.2` |
| `constraints-macos-py312.txt` | 2026-11-02 在干净 macOS arm64 / Python 3.12 环境安装验证的精确版本约束；不是其他平台的通用锁文件 |

以下是整理文档时 `.venv-mlx` 的安装快照，**不等于跨平台锁文件，也不表示所有范围内版本都已测试**：

| 依赖 | 本机版本 | 安装关系 |
| --- | --- | --- |
| FastAPI / Uvicorn | 0.162.0 / 0.54.0 | 基础依赖入口 |
| HTTPX / websockets | 0.28.1 / 15.0.1 | 基础依赖入口 |
| faster-whisper / CTranslate2 | 1.2.1 / 4.8.4 | 前者直接声明，后者由识别依赖引入 |
| mlx-whisper / MLX / mlx-lm | 0.4.3 / 0.32.4 / 0.32.2 | MLX 入口固定版本 |
| Pydantic / Starlette | 2.14.2 / 1.10.0 | 由 FastAPI 依赖链安装，代码直接使用 |
| NumPy / PyAV | 2.5.4 / 19.0.2 | 由识别依赖链安装，代码直接使用 |
| SciPy | 1.18.1 | MLX 识别相关依赖链；代码按需导入 |

`mlx-lm` 声明的 Python 下限为 3.11；本机快照中的 NumPy 2.5.4、PyAV 19.0.2 和 SciPy 1.18.1 均要求 Python 3.12 及以上。因此复现本机组合应使用当前核验的 Python 3.12.14 ARM64；Python 3.11 另行解析出的依赖组合尚未验收。旧 `.venv` 中的 Python 3.9.6 是历史兼容环境，不能作为新机器当前全量依赖的支持承诺。

模型相关的主要传递依赖如下；它们随推理库安装，应用没有另行提供训练服务：

| 依赖链 | 主要组件与用途 |
| --- | --- |
| `mlx-whisper` | PyTorch、Numba、tiktoken、SciPy、NumPy、Hugging Face Hub；用于库内部音频/分词/计算与模型支持，实际直播引擎仍为 MLX |
| `mlx-lm` | Transformers、SentencePiece、protobuf、PyYAML、Jinja2、NumPy；用于文字模型配置、分词与聊天模板 |
| `faster-whisper` | CTranslate2、tokenizers、ONNX Runtime、PyAV、Hugging Face Hub；当前直播在应用层切段，调用识别时关闭库内 `vad_filter` |
| 模型文件与通用工具 | safetensors、文件锁、缓存和进度条等由上述依赖链提供；实际安装版本以所用环境为准 |

发布准备已显式声明应用直接导入的 Pydantic、Starlette、NumPy、PyAV，并在新的 Python 3.12 环境中安装验证 macOS 版本约束。约束只限制实际安装的包，不强制安装环境快照中的无关包，也不适用于未验收的 Python／平台组合。

## 4. 目录与阅读顺序

```text
.
├── run.py                         # 本机服务入口，仅提供 --port 参数
├── 启动烤肉台.command               # macOS zsh 便捷启动
├── 安装macOS环境.command             # ARM64 Python 3.12 环境安装
├── requirements.txt               # 基础依赖
├── requirements-mlx.txt           # Apple Silicon 推理依赖
├── constraints-macos-py312.txt     # 已验证的 macOS 安装约束
├── yakiniku/                      # Python 后端
│   ├── server.py                  # API、WebSocket、应用装配与任务门禁
│   ├── audio.py                   # PCM 切段与 WAV 分卷
│   ├── live_pipeline.py           # 持久化直播处理与字幕补全
│   ├── model_runtime.py           # 本机 ASR 生命周期与推理线程
│   ├── local_translation*.py      # 本机翻译与模型目录/下载
│   ├── model_downloads.py         # ASR 显式权重下载
│   ├── provider.py                # 文字与旧语音协议适配
│   ├── import_audio.py            # 完整音频解码与后台处理
│   ├── file_transcriber.py        # 云端文件识别
│   ├── text_pipeline.py           # 日语校对与中文翻译
│   ├── refinement.py              # 直播录音的独立精修
│   ├── service_config.py          # 连接、模型路由、配置版本
│   ├── live_config.py             # 直播独立配置与快照
│   ├── storage.py                 # 设置、会话、恢复与字幕导出
│   └── space_downloads.py         # Space 任务、浏览器捕获与下载
├── static/                        # 多页面前端与音频 worklet
├── browser-extension/space-capture/# MV3 源码及其 GPL 许可证
├── tools/hls-audio-download/       # HLS CLI、集成测试、FFmpeg 来源说明
├── scripts/                       # 本机模型诊断、测速、扩展打包
├── tests/                         # Python 与 JavaScript 回归测试
├── docs/                          # 开发指南与功能记录
├── data/                          # 本机设置、权重、录音；已忽略
├── artifacts/                     # 本机验收截图/报告；已忽略
└── downloads/                     # 本机下载素材；已忽略
```

建议先看 `server.create_app()` 的依赖注入和路由装配，再看 `DurableLiveSession`、模型 runtime、`SessionStore`，最后按功能阅读对应前端页面。详细结构见 [后端架构](backend-architecture.md) 和 [前端与浏览器组件](frontend-and-extension.md)。

## 5. 新机器搭建

### 5.1 Apple Silicon 主路径

安装 ARM64 Python 3.12，确认进入 clone 后的项目根目录，再执行：

```sh
python3.12 -c 'import platform; print(platform.python_version(), platform.machine())'
python3.12 -m venv .venv-mlx
.venv-mlx/bin/python -m pip install -r requirements-mlx.txt -c constraints-macos-py312.txt
.venv-mlx/bin/python -m pip check
.venv-mlx/bin/python run.py
```

架构输出应为 `arm64`。打开 [本机工作台](http://127.0.0.1:8765)，并在相同 Edge 浏览器中打开要采集的直播页。项目目录可以包含中文或空格；在 shell 中使用完整路径时需要加引号。

服务安装成功不代表识别模型已经就绪。进入“直播设置”，显式下载 ASR 权重，再保存并加载/预热。若使用本机翻译，还需单独下载并加载 Qwen 权重，最后保存直播配置。云端翻译则先配置文字服务连接和模型。模型下载、加载、配置保存分别执行，不会相互替代。

也可双击根目录的 `安装macOS环境.command`，脚本检查 ARM64 Python 3.12 后安装同一组依赖；不下载模型。可通过 `.venv-mlx/bin/python run.py --port 8766` 更换端口。启动脚本优先使用 `.venv-mlx`，找不到时仅允许使用已准备的 `.venv` 兼容环境；没有环境时提示安装，不再使用 macOS 自带旧 Python 自动创建环境。脚本使用 Microsoft Edge 和默认 8765 端口。

### 5.2 基础环境与 CPU 兼容模式

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python run.py
```

此入口可运行服务与基础功能，不能提供 MLX 识别或本机 Qwen 翻译。使用 Tiny 直播需自行准备 CTranslate2 格式的 Tiny 模型到 `data/models/tiny/`（至少应有 `model.bin`，实际加载还需该模型的完整配套文件），并在直播设置中明确选择 Tiny、加载预热。页面没有 Tiny 一键下载入口；程序不会在 MLX 失败时自动下载 Tiny 或切换后端。跨平台完整行为仍待验证。

### 5.3 Space 下载的额外准备

文件导入通过 PyAV 解码，**Space 回放下载另需可执行的 FFmpeg 命令行程序**。网页后端优先使用显式设置的 `YAKINIKU_FFMPEG`；未设置时先查找仓库的 `tools/hls-audio-download/bin/ffmpeg`，再搜索系统 `PATH`。显式路径无效时直接报错，不静默更换。FFmpeg 二进制已被 Git 忽略，不随源码分发。

在项目根目录，为当前平台安装并确认 FFmpeg 后，重新启动服务即可使用 PATH 中的程序：

```sh
# macOS 已安装 Homebrew 时：
brew install ffmpeg
ffmpeg -version
.venv-mlx/bin/python scripts/package_space_capture.py
```

ZIP 生成在 `static/space-capture.zip`，可从网页下载；也可直接在 Edge 扩展管理页加载 `browser-extension/space-capture/` 源码目录。组件需手动加载，且对应浏览器账号应有该场回放的收听权限。详见 [Space 操作说明](space-audio-download.md)。当前 `bin/FFmpeg-NOTICE.txt` 只说明原本机二进制来源；自行安装的构建应以自身许可证为准。

## 6. 配置、模型与本机数据

应用默认在 `data/settings.json` 保存配置，文件权限为 `0600`。通过页面保存连接和模型路由即可；不要复制个人设置文件作为开源示例，也不要把密钥写入前端。

| 环境变量 | 实际作用 |
| --- | --- |
| `YAKINIKU_DATA_DIR` | 更改设置、录音和 Space 任务的数据目录；**不会迁移模型目录** |
| `OPENAI_API_KEY` | 旧共享配置的环境密钥；统一连接只有绑定 `use_environment_key` 时读取，不能替代逐连接配置 |
| `YAKINIKU_LOCAL_MODEL_PATH` | 旧本机识别/旧本机导入适配器的模型路径；不是新直播 MLX 的统一模型根目录 |
| `YAKINIKU_FFMPEG` | 可选的 FFmpeg 可执行文件路径；设置后网页 Space 下载只使用该路径 |
| `HF_HUB_OFFLINE` / `HF_HUB_DISABLE_IMPLICIT_TOKEN` | 本机推理路径在运行时设为 `1`，禁止隐式 Hub 下载或令牌使用 |
| `NUMBA_CACHE_DIR` | MLX ASR 默认设置为仓库 `data/runtime-cache/numba`，外部已设时保留 |

应用没有引入 dotenv，不会自动加载 `.env`。如自行使用环境变量，需由启动 shell 或进程管理器注入；修改运行环境后重启服务。

新直播模型在代码中默认定位至仓库 `data/models/`，本机翻译使用 `data/models/translation/`。`YAKINIKU_DATA_DIR` 指向其他目录时，这两个模型位置仍保持原样。ASR、本机翻译分别使用固定仓库版本和文件清单，下载经大小/SHA256 检查后安装；推理阶段读取本机文件，失败不会自动更换模型或云端服务。

会话数据位于 `data/recordings/<session-id>/`，包括原音频、`session.json`、SRT、VTT 和对照 TXT。时间轴以累计音频采样数计算，WAV 默认每三小时分卷，字幕使用整场时间。完整音频导入保留 `source.<扩展名>`；Space 任务单独保存在 `data/space-downloads/`。数据字段与恢复机制见 [后端架构](backend-architecture.md)。

## 7. 开发验证

安装 Node.js 后，在项目根目录运行。2026-10-31 使用的 Node 版本为 24.21.0；未单独验证更低版本支持范围。

```sh
.venv-mlx/bin/python -m pip check
.venv-mlx/bin/python -m unittest discover -s tests -v
node --test tests/*.test.cjs
```

本轮整理实跑结果：Python **435 项通过**；Node 从 **19 个 `.test.cjs` 文件**运行，测试器汇总 **30 项通过**。部分脚本还包含多个断言/场景，该汇总不等于全部业务场景数。主测试使用模拟服务、假权重和临时目录，不调用真实收费 API；测试通过不代表真实 GPU、网络延迟、日语准确率或浏览器共享行为已经验收。

HLS 媒体集成测试独立运行，需要 FFmpeg、OpenSSL 和绑定本机回环临时端口的权限。可指定 FFmpeg 路径：

```sh
HLS_TEST_FFMPEG="$PWD/tools/hls-audio-download/bin/ffmpeg" \
  .venv-mlx/bin/python -m unittest discover -s tools/hls-audio-download/tests -v
```

本轮在允许绑定本机回环端口的环境中实跑 **7 项通过**。缺少 FFmpeg 或 OpenSSL 时测试会跳过；应检查输出，不能把跳过记录为通过。用程序生成的音频测试 HLS 不等于已验证真实 X Space 回放。

真实模型诊断工具需要已下载的模型及普通本机终端的 GPU 权限：

```sh
# 静音加载检查，不证明语音准确率
.venv-mlx/bin/python scripts/check_mlx_asr.py

# 替换为自己有权限使用的 PCM16 WAV；报告写入已忽略的 data/diagnostics/
.venv-mlx/bin/python scripts/benchmark_live_asr.py \
  --audio "data/recordings/SESSION_ID/audio.wav" \
  --backend mlx --model mlx-whisper-small \
  --seconds 60 --segment-seconds 5
```

改动浏览器采集、停止保存或字幕显示后，还需手动检查授权共享、音量、双语显示、尾包保存、导出以及中断补全。当前未完成 10 分钟真实直播、30 分钟稳定性、Small/Turbo 对比及本机 Qwen 质量/峰值内存验收，不应以历史短样本数据承诺这些指标。

## 8. 修改代码的约定

1. 直播使用“本机 ASR → 一次中文翻译”；完整音频/精修使用“ASR → 日语校对 → 中文翻译”。保持两条链路的配置和模型选择独立。
2. 原始音频保存独立于识别/翻译成功与否；时间轴来自采样时钟。修改队列或超时前先检查补全、重启恢复和成功片段去重行为。
3. 新增服务商优先扩展协议适配，连接保存域名、密钥与协议，业务路由选择连接和模型。不要按厂商名称推定语音能力。
4. 不回显密钥，不在前端 localStorage 保存凭据，日志和会话快照也不得包含密钥。更换 API 目的地时保留现有的密钥重填与快照校验。
5. 新增模型需维护白名单、版本、文件大小、SHA256、显式下载/取消和本机准备门禁。保持本机推理串行执行及取消后的线程安全。
6. 原生前端通过服务端静态目录直接运行；修改 JS/CSS 后检查 HTML 的缓存版本参数和浏览器刷新效果。Node 模拟测试不能替代真实浏览器验收。
7. 针对业务逻辑修改运行相关回归测试，提交前运行主测试入口；文档、截图和样例只使用脱敏或合成数据。

## 9. 开源交付边界

目标仓库为 [fancyQu/YAKINIKU_STUDIO](https://github.com/fancyQu/YAKINIKU_STUDIO)，主代码采用 GPL-3.0-only；浏览器组件保留自己的许可和来源。已准备直接依赖声明、macOS 版本约束、干净环境安装检查与 GitHub Actions。本次交付为源码预览，不包含模型、FFmpeg 或签名应用安装包；真实直播稳定性和模型质量仍按上述边界验证。详见 [GitHub 开源发布检查](open-source-checklist.md)。
