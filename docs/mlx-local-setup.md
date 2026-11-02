# MLX 本机环境与模型准备记录

最初准备日期：2026-10-30（北京时间）；本机翻译说明更新于 2026-10-31。本文保留历史准备过程和当时的验证结果；新机器安装以 [当前开发指南](development.md) 为准。配套需求见 [直播开发交接](live-mlx-development-brief.md)，当前操作见 [直播本机中文翻译](live-local-translation.md)。

## 当前应用中的 ASR 手动下载

下文保留最初的环境准备与 Small 实测记录，所述“尚未接入”是当时状态。当前直播已接入独立本机模型流程，可在 **直播设置 → 本机日语识别 → MLX 模型下载** 操作。本节说明 Whisper ASR；**Qwen 翻译模型当前使用阿里 ModelScope 国内源**，入口见下一节，二者的来源和权重分别管理。

- 选择 Small（约 481 MB）或 large-v3-turbo（约 1.61 GB）后，先检查本机状态；只有用户点击 **下载模型** 才联网。页面展示 Hugging Face 仓库、固定版本、大小、下载与校验进度，支持取消及失败后手动重试。
- 下载在临时目录完成，逐文件校验大小及 SHA256 后才安装。已存在的完整模型不重复下载；不完整的已有目录保留并提示检查。
- 下载不会修改已保存的模型选择，不会加载、预热或开始录制。完成后由用户点击 **保存并加载模型**；工作台另有 **加载并预热** 按钮。
- 打开页面、切换选择、保存配置及录制不触发隐式下载，识别运行时继续只读本机权重。

本轮下载流程使用模拟文件完成自动化验证，未代用户下载真实模型；Small 的历史真实下载与短样本验证见下文。Turbo 尚未做真实下载或性能对比。

### 直播本机翻译入口

当前 **直播设置 → 简体中文翻译 → 翻译方式** 可选择云端或本机。本机文字推理使用 `requirements-mlx.txt` 中的 `mlx-lm`，模型目录提供 Qwen3 4B Instruct 2507、Qwen2.5 3B／1.5B／7B 的 4-bit 权重；这些文字权重与 Whisper ASR 权重分别管理，不能相互替代。历史环境版本表与 Small 测速只描述当时的 ASR 检查，不证明本机文字模型已经安装或通过测试。

使用本机翻译时，先选择模型，确认下载平台显示 **阿里 ModelScope（国内源）** 和仓库编号，再显式 **下载模型**，待校验完成后点击 **加载模型**，最后 **保存直播配置**。仓库组织名 `mlx-community` 不表示请求发往 Hugging Face，实际平台以页面所示来源为准。本机模式不需要 API 密钥；下载和加载不改变已保存的翻译方式或模型，云端连接与模型选择继续保留。打开页面、切换模型、保存和开始录制不会触发模型下载。缺少文字依赖时，在项目目录执行 `.venv-mlx/bin/python -m pip install -r requirements-mlx.txt`，然后重新启动服务。

本机翻译与 ASR 共用单线程推理 worker，同一时间执行一项本机推理；两个模型可以同时驻留内存，但不会同时启动 GPU 推理。云端翻译最多三段网络并发，本机翻译只使用一个翻译 worker。取消协程等待不代表原生推理已经终止，队列与补全会继续保留音频和日语，不能据此承诺比云端更低延迟。

本机翻译预计运行内存：4B 约 **3.5–5.5 GB**、3B 约 **2.5–4 GB**、1.5B 约 **1.5–2.5 GB**、7B 约 **5.5–8 GB**。这些均为单请求、约 2K tokens 以内短上下文的估算，包含 KV 缓存与推理运行时，**不包含 ASR、macOS、浏览器或其他应用**；下载体积以页面固定文件清单为准。4B 为默认推荐候选，内存较紧张可选 3B，准确率与峰值内存仍须本机验证。

本机模式的音频、日语文本、上下文和术语表留在本机；显式下载 Qwen 权重会访问所示 ModelScope 国内源，不上传直播内容。云端模式仍按所选连接发送日语文本。完整音频导入和用户确认后的 MiniMax 录音精修继续独立运行，直播本机翻译不会自动上传音频，也不会在失败时静默改用云端。详细步骤和限制见 [直播本机中文翻译](live-local-translation.md)。2026-10-31 改源前的 Qwen Hugging Face 失败尝试作为历史记录保留在该页，不代表当前下载源，也不能当作 ModelScope 下载已验收。

## 1. 已完成与开发边界

- 已建立独立 `.venv-mlx`，安装 MLX Whisper 和现有项目依赖。
- 已从 Hugging Face 官方仓库下载 Small 多语言 MLX 权重，固定版本并校验 SHA256。
- 已验证 Metal GPU 可用，完成本地录音短片段的离线转写。
- 新环境中现有 239 项 Python 测试全部通过；`pip check` 无依赖冲突。
- **尚未将 MLX 接入直播业务。** 现有 `.venv`、默认启动脚本、直播流程、服务商密钥和 MiniMax 文件路由均未迁移。
- Small 只是首个可用候选。尚未比较 Turbo，尚未人工校对准确率或进行完整直播测试。

## 2. 环境

| 项目 | 已验证值 |
| --- | --- |
| 机器 | Mac mini，Apple M4，16 GB 内存 |
| 系统 | macOS 15.8.1 |
| Python | 3.12.14，arm64 |
| 环境目录 | 项目根目录下的 `.venv-mlx/` |
| mlx-whisper | 0.4.3 |
| mlx / mlx-metal | 0.32.4 / 0.32.4 |
| 默认设备 | `Device(gpu, 0)`，Metal 可用 |
| 环境大小 | 约 1.4 GB |

创建环境使用的解释器：

```text
/absolute/path/to/python3.12
```

最初使用本机 Codex 提供的 Python 运行时，以上已替换为通用路径示例。venv 的基础解释器依赖创建时的本机路径，不应把此 venv 宣称为可搬移部署包；新机器或更换基础 Python 时应重建环境。

依赖入口：`requirements-mlx.txt`。本次实际安装的完整版本清单保存在本机 `artifacts/mlx-environment.lock.txt`，不随公开仓库提供；当前依赖和安装入口见开发指南。

```sh
# 在项目根目录执行
.venv-mlx/bin/python -m pip check
.venv-mlx/bin/python -m unittest discover -s tests
```

新环境应以开发指南和公开的依赖入口搭建；本机完整版本清单仅作历史核对，不能作为公开安装前提。不要在原 `.venv` 中直接升级依赖。

## 3. 模型

| 项目 | 值 |
| --- | --- |
| 类型 | Whisper Small，多语言，支持日语转写 |
| 官方托管仓库 | [mlx-community/whisper-small-mlx](https://huggingface.co/mlx-community/whisper-small-mlx) |
| 固定 revision | `45f3915923c7a79a5a5b5a7d909d39aeb0e5630e` |
| 本机目录 | 项目根目录下的 `data/models/mlx-whisper-small` |
| 权重文件 | `weights.npz`，481,307,592 字节，约 481 MB |
| 配置文件 | `config.json` |
| SHA256 | `55b6674c9b339702d486e2b1573839a66f8ec8f821ed2886993ef717a86b09f5` |
| 来源记录 | 模型目录下 `model-source.json` |

2026-10-30 准备 Small ASR 时，Hugging Face 初次连接超时，按用户要求重试后已成功。最终使用的是该官方托管仓库的固定版本，未使用返回 403 的镜像。这是既有 ASR 权重的历史来源；2026-10-31 的 Qwen 翻译权重下载已另行改用 ModelScope。

连接恢复前还从 MLX 官方转换脚本所列的 OpenAI CDN 下载了原始 Small checkpoint，校验通过，保留于 `data/model-downloads/whisper-small.pt`（约 484 MB）；其来源记录在同目录 `whisper-small-source.json`。它是备用原始权重，**当前 MLX 测试不依赖此文件，也未执行转换**。

模型和环境位于已忽略目录；业务代码中使用已下载的绝对路径，不能让录制开始时触发隐式 Hub 下载。

## 4. 可重复的离线检查

环境／模型加载检查，使用程序生成的静音：

```sh
# 在项目根目录执行
.venv-mlx/bin/python scripts/check_mlx_asr.py
```

这条命令只验证 GPU 和模型能否运行，静音结果不能证明语音准确率。

本次短片段测试的命令：

```sh
# 在项目根目录执行；将 SESSION_ID 替换为自己的场次编号
.venv-mlx/bin/python scripts/check_mlx_asr.py \
  --audio data/recordings/SESSION_ID/audio.wav \
  --start 0 --seconds 5 --runs 3 \
  --report data/diagnostics/mlx-small-asr-check.json
```

该工具只读取本机 PCM16 WAV，转为 16 kHz 单声道 float32，在本地识别；不调用翻译服务、不上传音频，设置 `HF_HUB_OFFLINE=1`。

在 Codex 的受限执行沙箱内，GPU 导入测试报告无 Metal 设备；通过允许访问本机 GPU 的执行环境运行后成功。开发者应使用普通本机终端或有 GPU 权限的执行环境验证，不能把沙箱限制误判为 M4 不支持 MLX。

## 5. 本次实测结果与限度

同一段 5 秒本机录音，先冷调用一次，再重复三次预热后调用：

| 指标 | 实测 |
| --- | --- |
| 冷调用 | 5.0090 秒，包含首次加载及处理 |
| 热调用 1 | 0.2053 秒 |
| 热调用 2 | 0.2052 秒 |
| 热调用 3 | 0.2080 秒 |
| 热调用中位数 | 0.2053 秒 |
| 实时因子 | 0.0411（耗时／音频时长） |
| MLX 分配内存峰值 | 1,226,232,750 字节，约 1.14 GiB |

原始结果保存在 `data/diagnostics/mlx-small-asr-check.json`，含本机转写文本和分段。报告未发送到外部。

这些数据仅说明该环境、该模型和这一个片段能完成转写：

- 热调用重复了同一段素材，不能据此推断任意直播内容或长时间稳定性。
- 三次样本不足以估计 P95。
- 不包含采集、停顿切段等待、队列、网络翻译和浏览器显示时间。
- 内存值是 MLX 分配峰值，不等于进程 RSS 或整机内存占用。
- `accuracy_verified=false`：尚未进行人工对照，未证明识别人名、数字、BGM 场景的准确率。
- 未与旧 Tiny 或 Turbo 做同条件比较，不能把 Small 宣称为实测最优模型。

## 6. 接入时需注意的接口

最小调用方式：

```python
import mlx_whisper

result = mlx_whisper.transcribe(
    audio_float32_16khz,
    path_or_hf_repo="/absolute/path/to/yakiniku/data/models/mlx-whisper-small",
    language="ja",
    task="transcribe",
    temperature=0.0,
    condition_on_previous_text=False,
    word_timestamps=False,
    verbose=None,
)
japanese = result["text"].strip()
```

- 生产接入仍需预热、单 worker、队列上限、时间区间和异常处理；示例不是完整直播实现。
- 返回的是字典，不能照搬 faster-whisper 的 `(segments, info)` 和段对象接口。
- 安装版本的 `beam_size` 非空会抛出 `NotImplementedError`；当前项目的 `beam_size=1/3` 不可直接传入。
- `vad_filter` 不是当前 MLX 通用接口参数。沿用应用自己的停顿切段及静音处理。
- `word_timestamps=True` 是库支持的选项，但本次未测试；本轮直播沿用应用采样区间定位。
- 不使用 `.en` 英语专用模型，不使用 Whisper 的英语翻译任务输出中文。

## 7. 本轮实际新增／修改文件

- `.gitignore`：仅增加 `.venv-mlx/` 忽略项。
- `requirements-mlx.txt`：独立依赖入口，包含现有项目依赖及固定 MLX 版本。
- `artifacts/mlx-environment.lock.txt`：本次安装的完整依赖版本。
- `scripts/check_mlx_asr.py`：可重复运行的本地环境／转写检查工具。
- `docs/live-mlx-development-brief.md`：业务需求、逻辑、边界及验收。
- `docs/mlx-local-setup.md`：本文件。

此外准备了忽略目录中的 `.venv-mlx`、模型、来源元数据和测试报告。没有修改 `yakiniku/`、`static/`、现有启动脚本或用户 API 配置。交由开发 agent 按需求完成接入后，才可把直播标记为使用 MLX。

## 8. 参考来源

- [Apple MLX Whisper 官方说明](https://github.com/ml-explore/mlx-examples/tree/main/whisper)
- [mlx-whisper 软件包](https://pypi.org/project/mlx-whisper/0.4.3/)
- [MLX 软件包](https://pypi.org/project/mlx/0.32.4/)
- [本次使用的固定模型版本](https://huggingface.co/mlx-community/whisper-small-mlx/tree/45f3915923c7a79a5a5b5a7d909d39aeb0e5630e)
