---
name: hls-audio-download
description: Download replay audio from M3U8/HLS links, 猫抓 exports, or X/Twitter Space webpage links through the local task-scoped browser component. Supports ordinary AES-128 with an available key and FFmpeg MP3 transcoding; does not record ongoing live streams.
---

# HLS 音频下载

用户提供 M3U8 链接或猫抓导出文本并要求下载音频时，使用随附的 `scripts/download_hls.py`。默认交付经过 FFmpeg **重新编码**的 MP3；需要剪辑或用户要求时，加 `--wav` 另存 PCM WAV。之前仅封装为 M4A 的版本在用户播放器中未能播放，重新编码后的 MP3 已实际播放成功。

## Space 网页链接

用户只给 `https://x.com/i/spaces/<场次编号>` 时，先检查当前烤肉台项目的「Space 下载」入口；项目根目录为同时包含 `run.py`、`yakiniku/` 和 `browser-extension/` 的目录，不依赖个人绝对路径。它依赖同一浏览器已加载 `browser-extension/space-capture` 组件、账号有该回放的收听权限，以及新版本机服务。操作和限制见项目内 `docs/space-audio-download.md`。不要把普通网页 URL 直接交给 M3U8 脚本。

使用本机页面 `/static/space-downloads.html` 提交该场链接，等待专用标签页捕获清单与本场密钥，后端自动下载、转码和整段验证。只查看指定 Space 和必要音频资源；用户不授权发帖、点赞、转发、私信等社交操作。不提取浏览器 Cookie。组件未连接时完成一次性设置；不要为了更新服务中断现有录制或转写，可以使用独立端口和临时数据目录验收。

真实播放是必要条件：无回放、无权限、播放器变化或完全在独立 Worker 内请求时，自动捕获可能失败。报告具体状态，必要时回到用户提供的猫抓导出方式；不承诺每个 Space 链接都能下载。新组件加载涉及浏览器访问权限，按当前工具要求在加载前处理确认。

## 执行

1. 将用户提供的链接和可选的密钥原文写入权限为 `0600` 的临时文本文件。用户已经附上文本文件时可直接读取；不要把完整链接、密钥、Cookie 打印到日志。不要沿用另一场直播的密钥。
2. 使用本技能目录内的脚本和持久保存的 `bin/ffmpeg`，选一个新的输出目录。脚本仅依赖 Python 3.9+ 标准库和 FFmpeg：

   ```sh
   python3 /absolute/path/to/hls-audio-download/scripts/download_hls.py \
     --input /private/tmp/hls-input.txt \
     --output-dir /absolute/path/to/output \
     --name audio
   ```

   `--input` 接受 M3U8 链接、含链接的文字或猫抓导出文本，并识别 `密钥(Hex)` / `密钥(Base64)`。也可以使用 `--url`。单独提供的密钥文件使用 `--key-file`，支持原始 16 字节、Hex 或 Base64；避免在命令参数中传递密钥。`--ffmpeg` 可指定其他可执行文件，`--wav` 生成 WAV 备用文件。

   浏览器已捕获清单正文时，可用 `--url` 指定相对 URI 的基址、`--playlist-file` 读取本地清单，并用 `--key-map-file` 读取 `{密钥URI: Base64}` JSON。此模式不重新请求原始清单或密钥接口；密钥文件同样设为 `0600`，完成后清理。
3. 脚本会解析音频播放列表、完整下载分片、保留每段 IV、用 FFmpeg 转成 MP3并做整段解码验证。必须等待成功退出并检查报告，不要把下载中的文件当作成品。若运行环境禁止网络，按正常工具权限流程申请连接用户提供的 CDN。
4. 交付可点击的绝对路径 MP3 文件链接，说明实际时长和已下载分片数量；如生成 WAV，附备用链接。只把播放列表覆盖的内容称为已完整下载。清理自行创建的临时输入副本，保留用户原始附件。

## 已验证的关键行为

- 同一 AES-128 密钥下，不同分片也可能使用不同 IV。必须保留每个 `EXT-X-KEY` 的作用范围，不能把猫抓页面显示的单个 IV 套用于全部分片。
- 一个 replay URL 可能只覆盖两分多钟。`EXT-X-ENDLIST` 表示该列表结束，不证明它包含整场直播；需要更长内容时，应获取完整回放列表。
- 单独的 M3U8 链接有时无法取得会员音频密钥。若密钥请求返回 401/403，向用户索取猫抓导出的 `密钥(Hex)` 或 `密钥(Base64)`，无需索取账号密码。收到密钥后继续使用同一输入流程。
- 工具默认拒绝未结束的直播清单，避免把当前窗口误当完整回放。对 SAMPLE-AES/DRM、工具明确不支持的加密布局，报告具体限制，不尝试绕过。
- 脚本失败时交代失败分片或缺失信息；不静默跳过缺片，不承诺恢复服务器已删除的音频，不重试过期授权到无限次。
