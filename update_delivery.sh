#!/usr/bin/env bash
# Update the default systemd installation from this unpacked release.
set -euo pipefail
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR=/opt/qq-math-bot
if [[ ${EUID} -ne 0 ]]; then
  echo '请从新安装包的解压目录运行 sudo bash update_delivery.sh。'
  exit 1
fi
if [[ "$SOURCE_DIR" == "$APP_DIR" ]]; then
  echo '请从新安装包的独立解压目录运行，不要直接覆盖已安装目录。'
  exit 1
fi
if [[ ! -x "$APP_DIR/.venv/bin/python" || ! -f "$APP_DIR/.env" || ! -f "$APP_DIR/data/tasks.sqlite3" ]]; then
  echo '未找到 /opt/qq-math-bot 中的默认安装、配置或题目数据库，尚未修改程序。'
  exit 1
fi
if [[ "$(systemctl show qq-math-bot -p User --value)" != qqbot ]]; then
  echo '服务运行账户不是预期的 qqbot，尚未修改程序。'
  exit 1
fi

update_files() {
  "$APP_DIR/.venv/bin/python" - "$SOURCE_DIR" "$APP_DIR" "$1" <<'UPDATE_PY'
from pathlib import Path
import os
import shutil
import stat
import sys
import tempfile

source, target = (Path(value).resolve() for value in sys.argv[1:3])
phase = sys.argv[3]
if source == target or phase not in ("check", "install"):
    raise SystemExit("更新目录或阶段无效，未修改程序。")
names = ("render.py", "render_worker.py", "render_service.py", "input_cache.py", "solver.py", "task_bot.py", "task_store.py", "search_tools.py", "settings.py", "pdf_tools.py", "ai.py", "qqmedia.py")
new_modules = {"input_cache.py"}
changes = []
for name in names:
    incoming, installed = source / name, target / name
    if (incoming.is_symlink() or installed.is_symlink() or not incoming.is_file()
            or (installed.exists() and not installed.is_file())
            or (not installed.exists() and name not in new_modules)):
        raise SystemExit(f"缺少普通程序文件 {name}，未修改程序。")
    data = incoming.read_bytes()
    compile(data, str(incoming), "exec")
    previous = installed.read_bytes() if installed.exists() else None
    metadata = installed.stat() if previous is not None else (target / "task_bot.py").stat()
    backup = installed.with_name(name + ".before-image-delivery")
    if backup.is_symlink() or (backup.exists() and not backup.is_file()):
        raise SystemExit(f"备份位置异常：{backup.name}，未修改程序。")
    if data != previous:
        changes.append((installed, data, previous, metadata, backup))

if phase == "check":
    for installed, data, previous, metadata, backup in changes:
        if previous is None:
            continue
        try:
            with backup.open("xb") as stream:
                stream.write(previous)
            shutil.copystat(installed, backup)
        except FileExistsError:
            pass
    print("十二个程序文件语法检查通过；原文件备份已保留，不覆盖旧备份，新增模块可首次安装。")
    raise SystemExit(0)

if any(previous is not None and not backup.is_file() for installed, data, previous, metadata, backup in changes):
    raise SystemExit("缺少更新前备份，尚未修改程序。")


def replace_file(path, data, metadata):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target, prefix=".delivery-update-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        os.chmod(temporary, stat.S_IMODE(metadata.st_mode))
        if hasattr(os, "chown"):
            os.chown(temporary, metadata.st_uid, metadata.st_gid)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


written = []
try:
    for installed, data, previous, metadata, backup in changes:
        replace_file(installed, data, metadata)
        written.append((installed, previous, metadata))
except BaseException:
    for installed, previous, metadata in reversed(written):
        if previous is None:
            installed.unlink(missing_ok=True)
        else:
            replace_file(installed, previous, metadata)
    print("更新中断，已恢复本次改动前的程序文件。", file=sys.stderr)
    raise
print(f"已更新 {len(changes)} 个程序文件；配置和题目记录保留。")
UPDATE_PY
}

# Validate all incoming modules and create backups before stopping the service.
update_files check
systemctl stop qq-math-bot
restart_service() {
  local code=$?
  trap - EXIT
  if ! systemctl start qq-math-bot || ! systemctl is-active --quiet qq-math-bot; then
    echo '服务启动失败，请查看 sudo journalctl -u qq-math-bot -n 100 --no-pager'
    exit 1
  fi
  if [[ $code -ne 0 ]]; then
    echo '更新未完成，已请求恢复机器人服务；请保留上方错误信息。'
  fi
  exit "$code"
}
trap restart_service EXIT
update_files install
echo '交付、多图输入、PDF 全文读取及 DeepSeek 官方搜索更新完成。请在 QQ 发送 /帮助 查看完整指令。'
