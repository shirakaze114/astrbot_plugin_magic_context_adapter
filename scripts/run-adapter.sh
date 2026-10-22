#!/usr/bin/env bash
# 启动 mc-adapter（historian 由本进程跑模型）。
#
# 渠道来源唯一：插件面板写入的 llm.json（由 AstrBot 插件根据所选 provider 生成）。
# 这里不再从某个固定 provider_source 推导凭据，也不提供任何默认模型兜底，
# 避免面板选了 A 渠道、后台却偷偷用 B 渠道。
set -euo pipefail

ROOT="${ROOT:-/root/data/mc-adapter}"
LLM_CONFIG="${MC_LLM_CONFIG:-${ROOT}/data/llm.json}"

if [ ! -f "${LLM_CONFIG}" ]; then
  echo "[run-adapter] 缺少 ${LLM_CONFIG}：请先在 AstrBot 插件面板选择 historian 模型" >&2
  exit 1
fi

# 仅做校验与展示，不读取/打印密钥
"${PYBIN:-/root/.local/share/uv/tools/astrbot/bin/python3}" - "${LLM_CONFIG}" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
h = d.get("historian") or {}
if not h.get("base_url") or not h.get("model"):
    sys.exit("[run-adapter] llm.json 缺少 historian.base_url/model")
print(f"[run-adapter] historian -> {h['base_url']} | {h['model']}")
PY

export MC_LLM_CONFIG="${LLM_CONFIG}"
export MC_HISTORIAN_ENABLED=1
export MC_ADAPTER_PORT="${MC_ADAPTER_PORT:-8317}"
export MC_CALL_TIMEOUT_SECS="${MC_CALL_TIMEOUT_SECS:-600}"
export MC_PROJECT_ROOT="${MC_PROJECT_ROOT:-${ROOT}}"
export RUST_LOG="${RUST_LOG:-info}"

BIN="${BIN:-${ROOT}/adapter/target/release/mc-adapter}"
exec "${BIN}"
