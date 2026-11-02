#!/bin/zsh -f
set -eu
unsetopt BG_NICE
cd -- "${0:A:h}"
if [[ "$(/usr/bin/uname -s)" != Darwin || "$(/usr/bin/uname -m)" != arm64 ]]; then
  print '当前 macOS 版本面向 Apple Silicon Mac，请使用原生 arm64 终端。'
  exit 1
fi
if [[ ! -d '/Applications/Microsoft Edge.app' && ! -d "$HOME/Applications/Microsoft Edge.app" ]]; then
  print '请先安装 Microsoft Edge；直播标签页音频采集需要在同一个 Edge 浏览器中操作。'
  exit 1
fi
if [[ -x .venv-mlx/bin/python ]]; then
  yakiniku_python=.venv-mlx/bin/python
  if ! "$yakiniku_python" -c 'import fastapi, uvicorn, httpx, websockets, faster_whisper, mlx_whisper' >/dev/null 2>&1; then
    print 'MLX 环境依赖不完整，请双击「安装macOS环境.command」修复依赖。'
    exit 1
  fi
  print '运行环境：MLX / Apple Silicon；直播模型需在工作台加载并预热。'
elif [[ -x .venv/bin/python ]]; then
  print '未发现 .venv-mlx，使用原兼容环境；不会自动把直播 MLX 配置改成 Tiny。'
  print '使用 MLX 直播需先准备 .venv-mlx，详见 docs/mlx-local-setup.md。'
  yakiniku_python=.venv/bin/python
  if ! "$yakiniku_python" -c 'import fastapi, uvicorn, httpx, websockets, faster_whisper' >/dev/null 2>&1; then
    print '兼容环境依赖不完整。建议双击「安装macOS环境.command」准备当前 MLX 环境。'
    exit 1
  fi
else
  print '尚未安装运行环境。请先安装 ARM64 Python 3.12，再双击「安装macOS环境.command」。'
  exit 1
fi
print '直播烤肉台：http://127.0.0.1:8765'
print '保持此终端窗口打开。退出请按 Control+C。'
if /usr/bin/curl --silent --fail http://127.0.0.1:8765/api/health >/dev/null 2>&1; then
  print '检测到已有服务，将打开该服务。若刚更新代码，请先在原启动终端按 Control+C，再重新启动。'
  open -a 'Microsoft Edge' http://127.0.0.1:8765
  exit 0
fi
(
  for attempt in {1..30}; do
    if /usr/bin/curl --silent --fail --max-time 1 http://127.0.0.1:8765/api/health >/dev/null 2>&1; then
      open -a 'Microsoft Edge' http://127.0.0.1:8765
      exit 0
    fi
    sleep 1
  done
  print '服务尚未就绪，请检查终端错误；启动后可手动打开 http://127.0.0.1:8765 。'
) &
exec "$yakiniku_python" run.py
