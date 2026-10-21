#!/usr/bin/env bash
# 手动更新 ck-mc：拉源码 → 编译 → 备份 → 原子切换 → 重启模块。
# 只在 /mc update 时执行，不做任何定时拉取。
#
# 设计约束（来自上游 README）：
#   mc 的 store 不支持跨单库迁移降级，所以更新前必须备份；
#   编译失败绝不切换，保留旧二进制。
set -uo pipefail

UPSTREAM="${UPSTREAM:-/root/data/mc-adapter/magic-context}"
BIN_DIR="${BIN_DIR:-/root/.local/share/cortexkit/bin}"
DATA_DIR="${DATA_DIR:-/root/.local/share/cortexkit/magic-context}"
CK="${CK:-/root/.local/share/cortexkit/cmd/ck}"
export http_proxy="${http_proxy:-http://192.168.1.23:8443}"
export https_proxy="${https_proxy:-http://192.168.1.23:8443}"

log() { echo "[mc-update] $*"; }

if [ ! -d "$UPSTREAM/.git" ]; then
  log "上游目录不存在：$UPSTREAM"
  exit 1
fi

log "拉取上游"
if ! git -C "$UPSTREAM" fetch --depth 1 origin >/dev/null 2>&1; then
  log "git fetch 失败（网络/代理问题），保持当前版本"
  exit 2
fi
git -C "$UPSTREAM" reset --hard origin/HEAD >/dev/null 2>&1

log "编译 ck-mc（release）"
if ! (cd "$UPSTREAM" && cargo build --release -p mc-module >/tmp/mc_build.log 2>&1); then
  log "编译失败，保留当前版本。最后几行："
  tail -20 /tmp/mc_build.log
  exit 3
fi

NEW_BIN="$UPSTREAM/target/release/ck-mc"
if [ ! -x "$NEW_BIN" ]; then
  log "编译产物不存在：$NEW_BIN"
  exit 4
fi
NEW_VER="$("$NEW_BIN" --version 2>&1 | head -1)"
log "新版本：$NEW_VER"

log "备份 store/context.db"
ts="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$DATA_DIR/backup"
for f in store.db context.db; do
  if [ -f "$DATA_DIR/$f" ]; then
    cp -f "$DATA_DIR/$f" "$DATA_DIR/backup/$f.$ts" || {
      log "备份 $f 失败，中止更新"
      exit 5
    }
  fi
done

log "切换二进制"
cp -f "$BIN_DIR/ck-mc" "$BIN_DIR/ck-mc.prev" 2>/dev/null || true
install -m 0755 "$NEW_BIN" "$BIN_DIR/ck-mc" || {
  log "安装失败，回滚"
  [ -f "$BIN_DIR/ck-mc.prev" ] && cp -f "$BIN_DIR/ck-mc.prev" "$BIN_DIR/ck-mc"
  exit 6
}

log "重启 magic-context 模块"
if [ -x "$CK" ]; then
  "$CK" module restart magic-context >/dev/null 2>&1 || log "重启失败，请手动检查 ck module status"
  sleep 2
  "$CK" module status magic-context 2>&1 | head -3
fi

log "完成：$NEW_VER"
