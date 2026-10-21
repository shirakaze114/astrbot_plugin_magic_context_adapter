#!/usr/bin/env bash
# mc-adapter 引导 / 安装脚本
#
#   ./bootstrap.sh install     安装 systemd 服务并启动（ck-subc + mc-adapter）
#   ./bootstrap.sh uninstall   停止并移除服务
#   ./bootstrap.sh status      查看服务状态
#   ./bootstrap.sh start|stop|restart
#   ./bootstrap.sh build       仅编译 adapter
#
# 说明：
#   - 不做任何定时更新；升级请用 AstrBot 指令 `/mc update`。
#   - ck-mc 由 ck-subc daemon 托管，本脚本只负责让 daemon 与 adapter 常驻。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UPSTREAM="${UPSTREAM:-${ROOT}/magic-context}"
BIN="${BIN:-${ROOT}/adapter/target/release/mc-adapter}"
CK="${CK:-/root/.local/share/cortexkit/cmd/ck}"
CK_SUBC="${CK_SUBC:-/root/.local/share/cortexkit/bin/ck-subc}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
ADAPTER_UNIT="mc-adapter.service"
SUBC_UNIT="ck-subc.service"
ASTRO_CFG="${ASTRO_CFG:-/root/data/config/abconf_9521f9da-e7c9-47fe-b77f-06e05d9b8062.json}"
SOURCE_ID="${SOURCE_ID:-deepseek}"
PYBIN="${PYBIN:-/root/.local/share/uv/tools/astrbot/bin/python3}"
ADAPTER_PORT="${ADAPTER_PORT:-8317}"
PROJECT_ROOT="${PROJECT_ROOT:-/root/data/mc-adapter}"

log() { printf '[bootstrap] %s\n' "$*"; }
die() { printf '[bootstrap] 错误: %s\n' "$*" >&2; exit 1; }

need_root() { [ "$(id -u)" -eq 0 ] || die "需要 root 权限"; }
need_systemd() { command -v systemctl >/dev/null 2>&1 || die "找不到 systemctl"; }

# ---------- 构建 ----------

cmd_build() {
  if [ ! -d "${UPSTREAM}/.git" ]; then
    log "拉取上游 magic-context"
    git clone --depth 1 https://github.com/cortexkit/magic-context.git "${UPSTREAM}"
  fi
  log "编译 mc-adapter (release)"
  ( cd "${ROOT}/adapter" && cargo build --release )
  [ -x "${BIN}" ] || die "编译产物缺失: ${BIN}"
  log "完成: ${BIN}"
}

# ---------- 依赖检查 ----------

preflight() {
  [ -x "${CK_SUBC}" ] || die "缺少 ck-subc：${CK_SUBC}（先执行 ck setup --with mc）"
  [ -x "${BIN}" ] || die "缺少 mc-adapter：${BIN}（先执行 $0 build）"
  [ -f /root/.config/cortexkit/subc.jsonc ] || die "缺少 subc.jsonc 配置"
  [ -f "${ASTRO_CFG}" ] || log "警告: 找不到 AstrBot 配置 ${ASTRO_CFG}，historian 将不可用"
}

# ---------- 服务定义 ----------

write_adapter_unit() {
  # historian 的模型凭据在启动时从 AstrBot 的 provider 配置读取，不写进 unit 文件
  cat > "${UNIT_DIR}/${ADAPTER_UNIT}" <<UNIT
[Unit]
Description=Magic Context adapter for AstrBot
After=network-online.target ck-subc.service
Wants=network-online.target ck-subc.service

[Service]
Type=simple
WorkingDirectory=${ROOT}
Environment=HOME=/root
Environment=MC_ADAPTER_PORT=${ADAPTER_PORT}
Environment=MC_PROJECT_ROOT=${PROJECT_ROOT}
Environment=MC_HISTORIAN_ENABLED=1
Environment=MC_CALL_TIMEOUT_SECS=600
Environment=RUST_LOG=info
ExecStart=/bin/bash ${ROOT}/scripts/run-adapter.sh
Restart=always
RestartSec=5
KillMode=mixed
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
UNIT
  log "写入 ${UNIT_DIR}/${ADAPTER_UNIT}"
}

write_subc_unit() {
  cat > "${UNIT_DIR}/${SUBC_UNIT}" <<UNIT
[Unit]
Description=CortexKit subc daemon (supervises ck-mc)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=HOME=/root
Environment=XDG_CONFIG_HOME=/root/.config
ExecStart=${CK_SUBC}
Restart=always
RestartSec=5
KillMode=mixed
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
UNIT
  log "写入 ${UNIT_DIR}/${SUBC_UNIT}"
}

# ---------- 动作 ----------

cmd_install() {
  need_root; need_systemd; preflight

  write_subc_unit
  write_adapter_unit

  systemctl daemon-reload
  systemctl enable "${SUBC_UNIT}" >/dev/null
  systemctl enable "${ADAPTER_UNIT}" >/dev/null

  log "启动 ck-subc"
  systemctl restart "${SUBC_UNIT}"
  # 等 daemon 写好 connection file
  for _ in $(seq 1 20); do
    [ -f /run/user/0/subc-connection.json ] && break
    sleep 0.5
  done

  log "启动 mc-adapter"
  systemctl restart "${ADAPTER_UNIT}"
  sleep 3

  cmd_status
  log "安装完成。升级用 AstrBot 指令：/mc update"
}

cmd_uninstall() {
  need_root; need_systemd
  systemctl stop "${ADAPTER_UNIT}" 2>/dev/null || true
  systemctl disable "${ADAPTER_UNIT}" 2>/dev/null || true
  rm -f "${UNIT_DIR}/${ADAPTER_UNIT}"
  systemctl stop "${SUBC_UNIT}" 2>/dev/null || true
  systemctl disable "${SUBC_UNIT}" 2>/dev/null || true
  rm -f "${UNIT_DIR}/${SUBC_UNIT}"
  systemctl daemon-reload
  log "已移除服务（数据与二进制保留）"
}

cmd_status() {
  need_systemd
  for u in "${SUBC_UNIT}" "${ADAPTER_UNIT}"; do
    printf '%-22s %s\n' "$u" "$(systemctl is-active "$u" 2>/dev/null || true)"
  done
  if [ -x "${CK}" ]; then
    "${CK}" module list 2>/dev/null | head -3 || true
  fi
}

cmd_start()   { need_root; systemctl start "${SUBC_UNIT}" "${ADAPTER_UNIT}"; }
cmd_stop()    { need_root; systemctl stop "${ADAPTER_UNIT}" "${SUBC_UNIT}"; }
cmd_restart() { need_root; systemctl restart "${SUBC_UNIT}"; sleep 2; systemctl restart "${ADAPTER_UNIT}"; }

case "${1:-install}" in
  install)   cmd_install ;;
  uninstall) cmd_uninstall ;;
  status)    cmd_status ;;
  start)     cmd_start ;;
  stop)      cmd_stop ;;
  restart)   cmd_restart ;;
  build)     cmd_build ;;
  *)
    echo "用法: $0 {install|uninstall|status|start|stop|restart|build}" >&2
    exit 64
    ;;
esac
