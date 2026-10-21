#!/usr/bin/env bash
# 启动 mc-adapter（host runner 模式，historian 由本进程跑模型）。
# LLM 凭据从 AstrBot 的 provider 配置里读取，不落盘、不打印。
set -euo pipefail

ASTRO_CFG="${ASTRO_CFG:-/root/data/config/abconf_9521f9da-e7c9-47fe-b77f-06e05d9b8062.json}"
SOURCE_ID="${SOURCE_ID:-deepseek}"
PYBIN="${PYBIN:-/root/.local/share/uv/tools/astrbot/bin/python3}"

eval "$("$PYBIN" - "$ASTRO_CFG" "$SOURCE_ID" <<'PY'
import json, sys
cfg_path, source_id = sys.argv[1], sys.argv[2]
d = json.load(open(cfg_path, encoding="utf-8-sig"))
src = {s.get("id"): s for s in d.get("provider_sources", []) if isinstance(s, dict)}
s = src.get(source_id) or {}
key = s.get("key") or ""
if isinstance(key, list):
    key = key[0] if key else ""
base = s.get("api_base") or ""
model = s.get("model") or ""
print(f"export MC_LLM_BASE_URL={json.dumps(base)}")
print(f"export MC_LLM_API_KEY={json.dumps(key)}")
print(f"export MC_LLM_MODEL={json.dumps(model or 'deepseek-chat')}")
PY
)"

export MC_HISTORIAN_ENABLED=1
export MC_ADAPTER_PORT="${MC_ADAPTER_PORT:-8317}"
export MC_CALL_TIMEOUT_SECS="${MC_CALL_TIMEOUT_SECS:-600}"
export MC_PROJECT_ROOT="${MC_PROJECT_ROOT:-/root/data/mc-adapter}"
export RUST_LOG="${RUST_LOG:-info}"

BIN="${BIN:-/root/data/mc-adapter/adapter/target/debug/mc-adapter}"
echo "[run-adapter] model=${MC_LLM_MODEL} base=${MC_LLM_BASE_URL} port=${MC_ADAPTER_PORT}"
exec "$BIN"
