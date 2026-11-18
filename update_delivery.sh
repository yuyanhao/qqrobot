#!/usr/bin/env bash
set -euo pipefail

cat >&2 <<'MESSAGE'
update_delivery.sh 已停用。旧脚本可能覆盖 PDF 页数校验和其他安全修复。
请下载完整版本源码包，在其目录中运行 sudo bash setup.sh；升级步骤见 docs/upgrading.md。
MESSAGE
exit 1
