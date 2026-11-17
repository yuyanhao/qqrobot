#!/usr/bin/env bash
set -euo pipefail

cat >&2 <<'MESSAGE'
update_pdf_input.sh 已停用。旧脚本会恢复过时的 PDF 读取策略，且可能覆盖当前程序文件。
请下载完整版本源码包，在其目录中运行 sudo bash setup.sh；升级步骤见 docs/upgrading.md。
MESSAGE
exit 1
