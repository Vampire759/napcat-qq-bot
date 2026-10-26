#!/usr/bin/env bash
# ============================================================
# start_ai.sh — 启动本地大模型推理服务 (llama.cpp llama-server)
#
# 特性:
#   • 自动识别显卡: 有可用 GPU 设备(支持 CUDA/ROCm/Metal/Vulkan 的 llama-server 构建)
#     就把全部层 offload 到显卡; 没有则自动退回纯 CPU 模式, 无需改代码。
#   • 模型与二进制路径可覆盖: 在本文件同目录的 conf/ai_local.sh 里写
#       LLAMA_BIN=/path/to/llama-server
#       LLAMA_MODEL=/path/to/model.gguf
#       LLAMA_PORT=11435
#     或用同名环境变量。
#
# 被 systemd 服务 llama-ai.service 调用。
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 可选覆盖配置文件(不入库)
if [ -f "$ROOT/conf/ai_local.sh" ]; then
    # shellcheck disable=SC1091
    . "$ROOT/conf/ai_local.sh"
fi

BIN_DEFAULT="$HOME/llamacpp/bin/llama-server"
if [ -x "$HOME/llamacpp-src/build-vulkan/bin/llama-server" ]; then
    BIN_DEFAULT="$HOME/llamacpp-src/build-vulkan/bin/llama-server"   # Vulkan 构建优先
fi
BIN="${LLAMA_BIN:-$BIN_DEFAULT}"
MODEL="${LLAMA_MODEL:-$HOME/models/gemma-3n-E2B-it-Q4_K_M.gguf}"
PORT="${LLAMA_PORT:-11435}"
CTX="${LLAMA_CTX:-4096}"
THREADS="${LLAMA_THREADS:-$(nproc 2>/dev/null || echo 4)}"
export LD_LIBRARY_PATH="$(dirname "$BIN"):${LD_LIBRARY_PATH:-}"

if [ ! -x "$BIN" ]; then
    echo "[start_ai] 找不到 llama-server: $BIN"
    echo "[start_ai] 请编译 llama.cpp 后设置 LLAMA_BIN, 或写 conf/ai_local.sh"
    exit 1
fi
if [ ! -f "$MODEL" ]; then
    echo "[start_ai] 找不到模型文件: $MODEL"
    exit 1
fi

# ---- 自动识别显卡 ----
NGPU=0
USE_SG=0
# AMD 显卡要能访问 /dev/dri/renderD* (通常属于 render 组); 用 sg 提权执行
if command -v sg >/dev/null 2>&1 && sg render -c true 2>/dev/null; then
    USE_SG=1
    DEV="$({ sg render -c "\"$BIN\" --list-devices"; } 2>/dev/null | tail -n +2 \
          | grep -viE '^\s*$|\(none\)|llvmpipe' || true)"
else
    DEV="$("$BIN" --list-devices 2>/dev/null | tail -n +2 \
          | grep -viE '^\s*$|\(none\)|llvmpipe' || true)"
fi
if [ -n "${DEV:-}" ]; then
    NGPU=999   # 全部层 offload; 显存不足时 llama.cpp 会自动回退
fi
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L 2>/dev/null | grep -qi gpu; then
    NGPU=999
fi

if [ "$NGPU" -gt 0 ]; then
    echo "[start_ai] 检测到显卡, 使用 GPU 推理 (ngl=$NGPU)$( [ "$USE_SG" = 1 ] && echo ' [sg render]')"
else
    echo "[start_ai] 未检测到可用显卡, 使用 CPU 推理 (ngl=0, threads=$THREADS)"
fi

ARGS=(-m "$MODEL" --host 127.0.0.1 --port "$PORT"
      -c "$CTX" -t "$THREADS" -np 1 -ngl "$NGPU" --jinja
      --alias "${LLAMA_ALIAS:-gemma-3n-e2b}")

if [ "$USE_SG" = 1 ] && [ "$NGPU" -gt 0 ]; then
    printf -v CMD '%q ' "$BIN" "${ARGS[@]}"
    echo "[start_ai] exec: sg render -c '$CMD'"
    exec sg render -c "exec $CMD"
fi
exec "$BIN" "${ARGS[@]}"
