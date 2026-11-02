# Space 回放音频下载

烤肉台的「Space 下载」使用浏览器中的播放权限取得本场回放，再在本机下载和转码为 MP3。它不要求复制账号密码或 Cookie，不会把音频发送给语音识别或翻译服务。

## 首次设置

1. 正常启动烤肉台，打开首页侧栏的「Space 下载」。
2. 在该页的「首次使用」中下载浏览器组件并解压。源码目录是项目下的 `browser-extension/space-capture/`。
3. 在 Edge 的扩展管理页面打开开发者模式，选择「加载解压缩的扩展」，加载上述目录。组件名称为「烤肉台 · 指定 Space 音频捕获」。加载是一次性的浏览器设置；工具不会自行安装或修改用户浏览器配置。
4. 刷新 Space 下载页面，确认「浏览器组件已连接」。在同一浏览器中登录有收听权限的 X 账号。
5. 安装独立 FFmpeg（例如 `brew install ffmpeg`）并重新启动服务。后端支持系统 PATH；也可通过 `YAKINIKU_FFMPEG` 指定可执行文件，模型和 FFmpeg 均不随源码提供。

## 下载

将 `https://x.com/i/spaces/…` 链接粘贴到 Space 下载页面，点击「打开回放并下载 MP3」。组件会打开本次任务专用的标签页，尝试点击本场的「播放录音」。如果浏览器没有自动开始播放，在该页手动点击一次。

任务先显示等待浏览器、捕获播放列表或等待本场密钥。资料齐全后，本机服务下载所有分片，按每段自己的 IV 解密，并用 FFmpeg 重新编码。验证完整时长和整段解码通过后，显示 MP3、试听播放器及完整性报告。

只保存播放列表覆盖的音频。未结束的直播、缺片清单、DRM、无回放或账号没有收听权限的内容不交付为完整回放。当前浏览器捕获范围为页面内 fetch/XHR；完全在独立 Worker 内请求的资源可能无法捕获。X 播放器变化、浏览器自动播放限制也可能需要手动播放或猫抓导出作为备用方式。**加载组件后仍需用真实回放验收，不能承诺任意 Space 网页链接都能下载。**

## 范围和保存位置

- 只有明确开始的下载任务及其专用标签页会被激活；播放列表和密钥必须关联本场 Space。
- 自动点击仅限本场的「播放录音」，没有发帖、点赞、转发或私信功能。组件不读取其他标签页、浏览记录、Cookie 或账号密码。
- 任务完成、取消或超时后停止捕获；密钥只用于本机临时处理，不显示在任务列表或日志。
- 下载任务与直播字幕项目独立，文件保存在 `data/space-downloads/<任务编号>/`。`audio.mp3` 是成品，`audio.json` 是脱敏验证报告，`job.json` 是任务状态。
- 正在运行的旧服务不会自动替换。更新后请结束手头的录制、转写和下载，再正常重启烤肉台；也可在独立端口用临时数据目录验收。

## 开源来源和验证

浏览器捕获部分参考 [xifangczy/cat-catch](https://github.com/xifangczy/cat-catch) 的 fetch/XHR 捕获方式，按 GPL-3.0 保留许可证、作者来源和修改说明。详细范围见 `browser-extension/space-capture/NOTICE.md`。它通过本机接口与后端联动，不把猫抓整个全站嗅探器嵌入服务。

修改组件后重新生成下载包：

```sh
python3 scripts/package_space_capture.py
```

相关验证：

```sh
.venv-mlx/bin/python -m unittest tests.test_space_downloads -v
node tests/space-capture.test.cjs
node tests/space-downloads-ui.test.cjs
```
