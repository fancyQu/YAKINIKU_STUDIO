# 前端、浏览器组件与 HLS 下载开发说明

本文依据 `static/`、`browser-extension/space-capture/`、HLS 下载脚本与相关测试整理。后端模块和持久化流程见 [后端架构](backend-architecture.md)，日常使用见 [README](../README.md)。

## 1. 技术栈与组织方式

| 层次 | 实际使用的技术 | 作用与边界 |
| --- | --- | --- |
| 页面与样式 | HTML5、原生 CSS、SVG 图标 | 多页面应用，由本机 FastAPI 提供静态文件 |
| 页面逻辑 | 原生 JavaScript、浏览器 ES modules | 直接加载源码；当前没有 React、Vue、TypeScript、npm 依赖或打包构建步骤 |
| 实时采集 | `getDisplayMedia`、Web Audio API、`AudioWorklet` | 用户选择浏览器标签页音频，生成录音和识别两路 PCM |
| 通信 | Fetch、WebSocket、BroadcastChannel、`window.postMessage` | 分别用于 HTTP 接口、音频与实时事件、同源字幕窗口、扩展桥接 |
| 浏览器组件 | Chromium Manifest V3、Service Worker、Chrome Extensions API | 在指定 Space 任务标签页获取播放列表与本场普通 AES-128 密钥 |
| 下载工具 | Python 标准库、FFmpeg 命令行 | 分片下载、本地 HLS 清单、解密、MP3 转码与整段解码校验 |
| 前端测试 | Node.js 内置 `assert`、`vm`、`node:test` | 模拟 DOM、浏览器接口与扩展环境，无第三方测试框架 |

页面和脚本使用 `?v=...` 查询参数更新浏览器缓存版本。修改共享模块时，应同步检查 HTML 与其他模块的引用；无需执行前端编译。

## 2. 页面和模块入口

| 页面 | 主脚本 | 职责 |
| --- | --- | --- |
| `/`、`/static/index.html` | `app.js` | 模型就绪检查、直播录音、实时字幕、项目列表、演示 |
| `/static/import.html` | `imports.js` | 文件选择、上传、处理进度与重试 |
| `/static/providers.html` | `providers.js` | 独立保存 ASR 和文字连接、获取模型、绑定业务模型 |
| `/static/live-settings.html` | `live-settings.js` | 直播 ASR、云端或本机翻译、显式模型下载与加载 |
| `/static/preview.html?id=<项目编号>` | `preview.js` | 分卷音频播放、字幕定位、显示语言、精准校验 |
| `/static/overlay.html` | `overlay.js` | 同源独立字幕窗口，接收工作台的完整显示状态 |
| `/static/space-downloads.html` | `space-downloads.js` | 扩展握手、创建/取消 Space 下载、进度轮询、试听与下载 |

共享模块：

- `project-utils.js`：请求与错误处理、项目状态、时间格式、字幕定位、分卷选择和文件 URL。
- `navigation.js` / `navigation.css`：页面导航与 `return_to` 来源链，仅接受同源白名单页面，保留预览项目编号等允许参数。
- `import-settings.js`、`subtitle-settings.js`、`local-translation-settings.js`：各功能设置面板。
- `style.css` 为公共样式，其余 CSS 为页面补充样式。

设置草稿保存在页面内存；ASR 与文字模块分别管理草稿和版本。前端不使用 localStorage 或 sessionStorage 保存 API 密钥。服务端配置才是持久化来源，刷新前需要保存草稿。

## 3. 直播采集与通信约定

### 3.1 音频采集

`app.js::startCapture()` 在按钮点击事件的用户手势中直接调用 `getDisplayMedia()`，随后才进行异步请求。采集要求用户选中带音频的标签页；没有音频轨道时明确失败。代码请求视频轨道以唤起浏览器共享选择器，音频处理仅从 `getAudioTracks()` 创建源，不录制画面。

`AudioContext` 请求 48 kHz，但以 `context.sampleRate` 的实际值创建会话。`audio-worklet.js` 在音频处理线程中完成：

1. 将输入浮点采样限幅并转换成 16 位 PCM；保留原采样率双声道，单声道输入复制成双声道。
2. 将左右声道均值重采样为连续的 24 kHz 单声道识别副本；跨渲染块保留分数采样状态，避免累计漂移。
3. 每约 100 ms 将录音与识别两个 `ArrayBuffer` 通过可转移对象交给主线程，同时报告 RMS 音量。
4. 输出节点写零，避免再次播放已由原标签页正常播放的声音。

24 kHz 是浏览器到服务端的识别流格式，不等于所有本机模型的最终输入采样率；模型适配器会继续转换。

### 3.2 WebSocket 协议

客户端先 `POST /api/sessions` 创建会话，再连接 `/ws/<会话编号>?token=<本场令牌>`。二进制消息使用一个字节前缀：

| 前缀 | 内容 |
| --- | --- |
| `0` | 原采样率、16 位 PCM 双声道录音 |
| `1` | 24 kHz、16 位 PCM 单声道识别副本 |

控制与状态消息是 JSON。前端处理 `ready`、`segment`、`transcript`、`translation`、`segment_status`、`warning`、`connection`、`progress`、`audio_ack`、`audio_saved` 和 `saved` 等消息；旧云端直播的 `partial` 仍有兼容显示路径。本机 MLX 使用确认后的分段结果，不应描述为逐字流式识别。

停止时，前端先向 Worklet 发送 `flush`，取得尾包完成通知；随后按 WebSocket 消息顺序发送帧数确认请求，等待服务端 `audio_ack`，再继续停止流程。`audio_saved` 与最终 `saved` 分开处理，音频完成保存后，字幕可以继续补全。修改停止逻辑时必须保留这一顺序。

WebSocket 发送缓冲超过 4 MiB 时，前端触发停止并尝试保存已接收录音。这个机制不能恢复尚未传到服务端的声音；断开后项目恢复流程仅处理已保存的内容。

### 3.3 字幕显示状态

字幕按 `session_id`、稳定段编号、时间和 `sequence` 更新。逐句列表最多保留 300 段显示记录，完整数据由后端保存。新的日语可以先显示为等待翻译；较晚返回的旧译文补入历史列表，主字幕不倒退，也不把不同段的日语与中文拼成一组。

工作台通过同源 `BroadcastChannel("yakiniku-captions")` 发布完整字幕显示快照，包含会话、修订号、双语文本和录制状态。`overlay.js` 只消费完整快照，拒绝旧修订及已退出场次的消息。独立字幕窗口依赖工作台页面和相同来源；它没有独立的后端订阅连接。

项目预览通过音频播放时间定位字幕；分卷位置保留整场时间。自动跟随只滚动逐句列表容器，用户手动滚动后可以暂停跟随。修改这一行为时应检查 `preview-scroll.test.cjs`。

## 4. Space 浏览器组件

### 4.1 权限和运行环境

[`manifest.json`](../browser-extension/space-capture/manifest.json) 声明 Manifest V3、最低 Chromium 111、`storage` 权限，以及 `https://x.com/*`、`https://twitter.com/*`、`http://127.0.0.1/*`、`http://localhost/*` 的主机权限。没有声明 `cookies`、`debugger`、`webRequest`、`scripting` 或全站权限。

主机权限覆盖上述站点和本机地址；实际本机命令入口进一步限制为 `/static/space-downloads.html`。这两个范围应分别向安装者说明。组件由用户在 Edge/Chrome 的开发者模式中加载解压目录，不会由项目自动安装。

### 4.2 组件分工与任务链路

| 文件 | 职责 |
| --- | --- |
| `rules.js` | Space URL、任务、CDN、密钥 URL、已结束播放列表与播放按钮校验 |
| `local-bridge.js` | 在本机下载页转发同源 `postMessage` 与 `chrome.runtime` 消息 |
| `background.js` | Service Worker 管理专用标签页、任务令牌、超时和捕获结果上传 |
| `x-bridge.js` | 在隔离世界取得任务配置，激活页面捕获，转发结果，尝试点击本场播放录音 |
| `page-capture.js` | 在页面 MAIN world 包装 fetch/XHR，检查指定任务相关响应 |
| `popup.html` | 范围说明和本机页面入口提示 |

流程为：下载页向后端创建任务 → 本机桥接通知扩展 → Service Worker 打开专用 Space 标签页 → 页面内响应确定本场媒体及 CDN 范围 → 捕获已结束媒体清单和匹配密钥 → Service Worker 带 `X-Space-Capture-Token` 发往本机 `/api/space-downloads/<任务编号>/capture` → 后端下载转码。

页面包装器在 `document_start` 注入，未激活时透传请求，任务激活后才检查符合范围的响应。fetch 使用克隆响应读取，不吞掉播放器原响应；XHR 监听载入结果。不会改写 Worker。任务带随机 nonce、专用标签页与 10 分钟有效期，最多同时保留 4 个正在启动或活动捕获任务。完成、取消、超时或标签页离开 X 后停止；停用时尝试恢复原 fetch/XHR 方法。

`chrome.storage.session` 保存恢复任务所需的任务元数据和捕获令牌；播放列表正文和媒体密钥不写入扩展存储或控制台。捕获结果还要在后台再次检查来源、nonce、Space 关联和播放列表规则，再交给本机服务。

自动播放只匹配本场对话框或菜单中的单个“播放录音 / Play recording / 録音を再生”按钮。浏览器自动播放限制、X 页面变化、无回放或缺少收听权限，以及请求完全发生在独立 Worker 中，都可能导致失败，需要手动播放或采用用户提供的导出清单。自动化测试不证明任意真实 Space 都可下载。

### 4.3 打包

在仓库根目录运行：

```sh
python3 scripts/package_space_capture.py
```

脚本检查并打包固定的 9 个文件，生成 `static/space-capture.zip`：清单、5 个 JS 文件、`popup.html`、`LICENSE`、`NOTICE.md`。它不读取浏览器配置、任务数据或凭据。可直接加载源码目录，也可解压 ZIP 后加载。ZIP 是可再生成产物，发布时应与源码版本一致。

## 5. HLS 下载与 FFmpeg

`tools/hls-audio-download/scripts/download_hls.py` 是独立 CLI，Python 部分只使用标准库，包括 `urllib.request`、`ThreadPoolExecutor`、`tempfile`、`subprocess` 和 JSON/Base64 处理。AES-128 解密和编解码交给 FFmpeg，不依赖浏览器录制正在播放的声音。

脚本支持 M3U8、猫抓导出文本、本地媒体清单和密钥映射；按主清单选择音频媒体，下载分片到临时目录，保留每段的密钥和 IV 作用范围。默认 4 个下载线程、每资源最多 3 次尝试。它拒绝未含 `EXT-X-ENDLIST` 的直播清单、显式缺片 `EXT-X-GAP`、低延迟部分分片、SAMPLE-AES/DRM，以及不支持的布局。`ENDLIST` 只表示本清单已结束，不能证明清单覆盖整场直播。

默认成品为 `libmp3lame` 编码的 192 kb/s、44.1 kHz 双声道 MP3，CLI 的 `--wav` 额外输出 48 kHz 双声道 PCM WAV。转码后再次完整解码，核对清单时长，偏差超过 2 秒则不交付成品；JSON 报告记录分片、时长和解码结果。程序不覆盖已有同名输出，不静默跳过失败分片。临时密钥文件权限为 `0600`，使用后清理。

FFmpeg 的查找方式目前存在入口差异：

| 入口 | FFmpeg 来源 |
| --- | --- |
| 独立 CLI | `--ffmpeg` 指定路径；未指定时依次查工具 `bin/ffmpeg`、系统 PATH |
| Space 网页后端 | 只查仓库 `tools/hls-audio-download/bin/ffmpeg`，其次查本机 Codex 技能目录；当前不查 PATH |

因此，GitHub 新克隆环境要使用 Space 网页入口，需要在 `tools/hls-audio-download/bin/ffmpeg` 准备适合本机系统、带 `libmp3lame` 的可执行文件。该二进制被工具目录 `.gitignore` 排除，不能假设 clone 后已经具备。后续可通过显式配置统一查找方式；当前文档不将其描述为已实现。

CLI 使用示例（输入文件可能含临时链接或密钥，请置于仓库外并限制权限）：

```sh
python3 tools/hls-audio-download/scripts/download_hls.py \
  --input /tmp/hls-input.txt \
  --output-dir /tmp/hls-output \
  --name audio \
  --ffmpeg /absolute/path/to/ffmpeg
```

Space 普通网页 URL 不是 M3U8，不能直接传给独立下载 CLI。网页 URL 需要先经过浏览器组件捕获。

## 6. 前端与组件验证

无需 `npm install`。使用包含内置 Fetch/Response 等接口的现代 Node.js；建议使用当前受支持的 LTS 版本。单项测试可直接运行：

```sh
node tests/worklet.test.cjs
node tests/caption-state.test.cjs
node tests/live-frontend.test.cjs
node tests/live-preview.test.cjs
node tests/preview-scroll.test.cjs
node tests/providers-ui.test.cjs
node tests/live-settings-ui.test.cjs
node tests/local-translation-ui.test.cjs
node tests/navigation.test.cjs
node tests/space-capture.test.cjs
node tests/space-downloads-ui.test.cjs
```

全部前端/扩展测试可在仓库根目录运行：

```sh
node --test tests/*.test.cjs
```

HLS 工具测试独立于主测试目录：

```sh
python3 -m unittest discover -s tools/hls-audio-download/tests -v
```

现有 Node 测试包含真实源代码执行及模拟浏览器/扩展行为，也有静态约束断言；它们不是真实浏览器端到端测试。音频共享选择器仍需用户操作。发布前还需手动检查浏览器标签页音频、macOS 权限、真实 Space 回放、停止尾包和长时间播放；不能把 VM 测试通过视为上述行为全部验收。

## 7. 开源发布中的第三方边界

- `browser-extension/space-capture/` 明确采用 **GPL-3.0-only**。fetch/XHR 检查方式改编自 [Cat Catch](https://github.com/xifangczy/cat-catch)，上游提交和修改说明保存在 [`NOTICE.md`](../browser-extension/space-capture/NOTICE.md)，GPL 文本保存在其 `LICENSE`。发布源码或 ZIP 时必须保留这些文件及源码中的 SPDX 标识。
- 工具现存 [`FFmpeg-NOTICE.txt`](../tools/hls-audio-download/bin/FFmpeg-NOTICE.txt) 记录了本机 FFmpeg 7.1 来自 `imageio-ffmpeg 0.6.0` 的 macOS arm64 wheel，并标注该构建为 **GPL-2.0-or-later**。Python 包自身的 `imageio-ffmpeg-LICENSE.txt` 不能替代 FFmpeg 二进制的许可义务。更换构建后应重新核对版本、编译特性、许可及相应源码提供方式；不能把本机二进制直接视作跨平台安装包。
- 根项目许可证尚需由维护者确定；新增根许可证不能改写上述第三方组件的既有许可。模型权重也有各自许可，不随前端源码许可自动改变。
- `tools/hls-audio-download/SKILL.md` 是供 Codex 集成使用的工作流指引，当前已使用通用项目定位；普通 CLI 使用方式见本页。音频、任务数据、临时清单、密钥和实际浏览器配置不属于源码发布内容。
