#!/usr/bin/env bash
# 拉取上游 Magic Context 并编译 adapter。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UPSTREAM="${ROOT}/magic-context"

if [ ! -d "${UPSTREAM}/.git" ]; then
  echo "==> cloning upstream magic-context"
  git clone --depth 1 https://github.com/cortexkit/magic-context.git "${UPSTREAM}"
else
  echo "==> upstream exists, fetching"
  git -C "${UPSTREAM}" fetch --depth 1 origin
  git -C "${UPSTREAM}" reset --hard origin/HEAD
fi

echo "==> building adapter (release)"
cd "${ROOT}/adapter"
cargo build --release

echo
echo "done: ${ROOT}/adapter/target/release/mc-adapter"
echo "run:  MC_STORE_DIR=/var/lib/mc-adapter ${ROOT}/adapter/target/release/mc-adapter"
