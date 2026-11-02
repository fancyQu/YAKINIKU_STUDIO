# MLX 直播接入实现与验证记录

日期：2026-10-30（北京时间）。对应需求：[live-mlx-development-brief.md](live-mlx-development-brief.md)。

## 本轮已实现

直播使用独立路径：**Edge 标签页音频 → 本机 MLX 日语识别 → 一次简体中文翻译**。MLX 以 `language="ja"`、`task="transcribe"` 处理 16 kHz 单声道 float32，不调用 Whisper 英语翻译任务。日语原稿保留在 `ja_raw` 和 `ja`，不串行调用独立日语校对。文件导入继续保留 MiniMax 文件 ASR、日语校对、中文翻译和三轨字幕导出。

本机权重通过绝对路径加载，准备前检查配置和权重、来源元数据与权重校验；真实 Metal 运算检查、模型加载和静音预热成功后才进入 `ready`。状态分为 `not_prepared`、`loading`、`ready`、`failed`。录制中不会下载权重或自动换模型。单个专用执行线程拥有模型，GPU 推理结束前不能通过取消调用释放模型并启动第二份推理；准备被页面取消后仍在后台继续，期间录制、导入、再次准备与配置保存互斥。

使用当前已准备的 Small 多语言模型作为初始候选，不宣称它优于 Turbo。Tiny 仍可显式选择为 CPU 兼容后端；Turbo 未安装，需要独立准备和实测比较。本轮没有自动下载新模型。

## 启动和首次配置

```sh
# 在项目根目录执行
.venv-mlx/bin/python run.py
```

也可双击 `启动烤肉台.command`。脚本优先使用 `.venv-mlx`，缺少该环境时说明兼容启动情况，不静默将直播配置改成 Tiny。脚本发现 8765 已有服务时只打开页面，不会替换旧进程；切换运行环境应先确认所有录制、导入与字幕补全结束，再正常停止原服务并重启。

打开 <http://127.0.0.1:8765/static/live-settings.html>：

1. 选择本机后端与模型。已准备 Small 的 ID 为 `mlx-whisper-small`，后端为 `mlx`。
2. 选择已有文字服务连接和直播翻译模型；连接地址、密钥与协议在「校对与翻译」模块管理。直播不要求独立校对或文件 ASR 配置。
3. 按需填写直播术语表，首轮保留最长 5 秒、停顿 600 ms 基线，也可选择 3 秒对比。
4. 保存并准备模型，等待「已就绪」，返回工作台。系统共享选择器由用户自己选择直播标签页并勾选音频。

模型准备与环境来源详见 [mlx-local-setup.md](mlx-local-setup.md)。MLX 需要本机 Apple GPU；受限沙箱可能报告 GPU 不可用，不能据此判定机器不支持。环境不可搬移时需按准备记录重建，不能把 `.venv-mlx` 当成独立安装包。

## 配置隔离和场次快照

设置文件增加 `live_settings`，包括 `backend`、`model`、`translation_route`、`glossary`、`segment_seconds`、`silence_ms`。旧字段与文件 `task_routes.file_asr/correction/translation` 保留；首次独立保存前只读预览现有翻译选择，保存后直播设置具有独立优先级。保存直播设置只修改 `live_settings`，不会重建密钥或覆盖文件任务配置。

场次 `live_snapshot` 只保存非敏感引用、参数和模型来源。快照包含翻译连接 ID、模型、地址／协议指纹、模型路径与来源版本，不包含 API Key 或完整 settings。凭据在运行或补全时从本机连接解析。重试时继续使用原场次模型和路由；连接删除、地址或协议改变，或本机权重版本不符时明确报错，避免静默换服务或模型。当前直播仍引用的连接不能直接从服务商配置删除。

主要本机接口：

| 接口 | 用途 |
| --- | --- |
| `GET/PUT /api/live-settings` | 读取／保存独立直播配置 |
| `GET /api/live-model/status?backend=mlx&model=mlx-whisper-small` | 查询实际加载状态、来源和已安装候选 |
| `POST /api/live-model/prepare` | 检查、加载和预热指定 `{backend, model}` |
| `POST /api/sessions` | 只有翻译配置完整且选定本机模型就绪才创建新场次 |
| `POST /api/sessions/{id}/retry-translations` | 幂等补全直播缺失阶段；导入项目保持文件流程 |

`translation_ready` 与「已配置」表示必要字段完整，不代表已通过真实 API 请求或模型能力验证。

## 保存、队列与恢复

原始 PCM 继续写入原有 WAV，可分卷；字幕时间从累计采样数计算。识别输入由应用按停顿和最长时长切分，每个提交区间有稳定 ID 和递增序号。建立可恢复任务前先将对应音频同步到磁盘。ASR 队列上限 3，翻译排队上限 6，翻译并发 3；超过实时预算的旧任务标记 `deferred`，不无界堆积或无记录丢弃。

日语完成后立即发出原稿事件，译文返回时更新同一段。历史、导出与主字幕使用同一 ID；乱序旧译文不覆盖较新的主字幕，工作台与独立字幕窗口共享配对状态。静音不生成字幕，确认无语音使用 `no_speech`／`skipped` 终态；有声却空响应、异常重复等保留为失败，不扩写成句子。

正常停止时浏览器 flush 尾包，后端返回累计已接收帧数，再停止采集。音频关闭并确认保存后立即发出 `audio_saved`；字幕继续补全，最后发出 `saved`。原始音频已收到但识别副本未到的尾区间，从原始 WAV 进行本机补识别。未传给后端的音频不能承诺已保存。

停止后的延期任务按音频顺序处理；已识别的段只补翻译，未识别段才补 ASR。成功字幕不重复调用。失败请求保留错误摘要供显式重试，不反复消耗账户请求；中文 SRT 只包含成功译文。重启将中断项目标记为可恢复，并从实际 WAV 恢复已接收音频与未提交尾区间。

没有快照的旧直播项目首次重试绑定当前直播翻译路由，保留已有校对日语、原稿与成功译文，仅补缺失翻译，不重跑旧 ASR 或校对。MiniMax 文件精修是独立流程，不会自动收到直播录音。

## 可复现验证

自动化测试使用本地适配器与模拟文字服务，不会上传用户音频或产生 API 费用：

```sh
.venv-mlx/bin/python -m unittest discover -s tests
node tests/live-settings-ui.test.cjs
node tests/providers-ui.test.cjs
node tests/worklet.test.cjs
node tests/caption-state.test.cjs
node tests/settings-ui.test.cjs
node tests/project-preview.test.cjs
node tests/preview-scroll.test.cjs
node tests/import-settings.test.cjs
node tests/cloud-import-ui.test.cjs
```

新增 `test_live_config.py` 覆盖路由独立、设置保留、快照和凭据边界；`test_live_api.py` 覆盖模型门禁、无需文件 ASR／校对、尾包确认、音频先保存、原始尾区间补回、重启恢复、旧项目幂等重试、模型版本校验及模型准备互斥。模型适配器另测取消与单 worker 行为。具体测试总数以最终运行结果为准。

真正经过本轮生产识别适配器的离线短样本测速使用 `scripts/benchmark_live_asr.py`。可复现命令和结果见 [live-asr-runtime-validation.md](live-asr-runtime-validation.md)。报告只记录计数和耗时，不输出转录正文；示例音频仅在本机读取，不调用翻译。3 秒与 5 秒各使用同一本地录音的前 60 秒，均出现了正常无语音或异常空响应／重复结果；异常片段未作为成功字幕发布。成功片段的短推理耗时不能代表整场字幕延迟或识别准确率。

## 尚未完成的验收与限制

最终回归记录（2026-10-30）：新 `.venv-mlx` 与原 `.venv` 各通过 289 项 Python 测试；11 个 Node 测试入口均通过（直播设置包含 6 个界面场景）。已在实际 Edge 页面完成 MLX GPU 模型加载与预热，确认就绪门禁；没有替用户选择直播源或上传既有录音。部署前后 `settings.json` 的 SHA256 相同，已有 8 个已完成项目保留。

- 尚无连续 10 分钟实际 Edge 直播、30 分钟稳定性与积压记录；离线 60 秒样本不能替代。
- 排错时已验证一次固定日语探针和 39 段实际补译请求，尚未验收真实直播中的中文显示延迟，429 和鉴权故障仍只有模拟验证。
- 尚未人工核对日语原音、人名、数字、游戏术语、短应答及带 BGM 的准确率，测速报告为 `accuracy_verified=false`。
- 尚未安装和比较 large-v3-turbo；本轮不根据模型名称选定最终默认值。
- 需求中的日语 P95 ≤ 1.5 秒、中文 P95 ≤ 3 秒仍是待实测目标，不是已达到的端到端承诺。还需结合片段等待、ASR 排队、翻译排队与浏览器显示统计「话音结束到中文显示」。
- 当前为分段字幕，无逐字对齐、说话人分离与专业人工校对。音乐、多人重叠讲话或低置信度内容仍可能失败，应保留原音频检查。

回退时在直播设置明确选择 `Tiny · CPU 兼容模式`，保存并预热。不得通过失败后的隐式回退改变正在录制的模型；文件业务设置不需跟着变更。

## 2026-10-30：翻译接口路径修复

一次直播的 39 个已识别片段均报解析失败。最小固定日语探针确认：服务商根地址下的 `/chat/completions` 返回 HTTP 200、`text/html` 管理网页，正确 `/v1/chat/completions` 返回标准 JSON 和有效中文。修正的是已保存连接的 API 基础路径，原模型和密钥保留。程序新增 HTML 响应诊断，覆盖三个协议和三种响应媒体类型的回归测试；完整 Python 测试共 290 项通过。

仅为这场全无译文的失败项目记录同一服务商的路径修正，原始 `live_snapshot` 保存于 `translation_connection_repair.original_snapshot`，没有放宽通用的连接固定校验。录音和已成功识别日语保留，补译 39 段全部成功；另 2 段本机识别仍为空响应或异常重复，继续保留失败状态与原音频，不伪造字幕。

39 次补译请求耗时中位数约 1.82 秒，最大约 4.64 秒，其中 1 次超过 4 秒实时请求预算。这是停止后的补译统计，不含采集、切段、排队、浏览器绘制，不是直播端到端延迟。完整长时直播及人工质量验收仍未完成。
