#!/bin/zsh -f
set -eu
cd -- "${0:A:h}"

if [[ "$(/usr/bin/uname -s)" != Darwin || "$(/usr/bin/uname -m)" != arm64 ]]; then
  print '当前版本需要 Apple Silicon Mac，请在原生 arm64 终端运行。'
  exit 1
fi

yakiniku_setup_python=''
for candidate in .venv-mlx/bin/python python3.12 /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import platform, sys; sys.exit(not (sys.version_info[:2] == (3, 12) and platform.machine() == "arm64"))' >/dev/null 2>&1; then
    yakiniku_setup_python="$candidate"
    break
  fi
done
if [[ -z "$yakiniku_setup_python" ]]; then
  print '请先安装 ARM64 Python 3.12，再重新双击此文件。'
  print '可使用 python.org 的 macOS 安装包，或已安装 Homebrew 时执行：brew install python@3.12'
  exit 1
fi

if [[ ! -x .venv-mlx/bin/python ]]; then
  "$yakiniku_setup_python" -m venv .venv-mlx
fi
if ! .venv-mlx/bin/python -c 'import platform, sys; sys.exit(not (sys.version_info[:2] == (3, 12) and platform.machine() == "arm64"))' >/dev/null 2>&1; then
  print '已有 .venv-mlx 不是 ARM64 Python 3.12 环境。请先备份并移走该环境，再重新安装。'
  exit 1
fi

print '正在安装 macOS 依赖；不会下载模型、读取 API 密钥或上传录音。'
.venv-mlx/bin/python -m pip install -r requirements-mlx.txt -c constraints-macos-py312.txt
.venv-mlx/bin/python -m pip check
.venv-mlx/bin/python scripts/package_space_capture.py
print '环境安装完成。请双击「启动烤肉台.command」，再在直播设置中手动下载并加载模型。'
