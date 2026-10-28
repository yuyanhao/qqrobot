#!/usr/bin/env bash
# One-time repair for the deployed systemd installation. No model calls.
set -euo pipefail
APP_DIR=/opt/qq-math-bot
if [[ $# -eq 0 ]]; then
  set -- Q261027-000011 Q261027-000012
fi
if [[ ${EUID} -ne 0 ]]; then
  echo '请用 sudo bash repair_pdf.sh Q261027-000011 Q261027-000012 运行。'
  exit 1
fi
for task_id in "$@"; do
  if [[ ! "$task_id" =~ ^Q[0-9]{6}-[0-9]{6,10}$ ]]; then
    echo '题号格式不正确。'
    exit 1
  fi
done
if [[ ! -f "$APP_DIR/render.py" || ! -f "$APP_DIR/render_service.py" || ! -x "$APP_DIR/.venv/bin/python" || ! -f "$APP_DIR/data/tasks.sqlite3" ]]; then
  echo '未找到 /opt/qq-math-bot 中的程序、Python 环境或题目数据库。'
  exit 1
fi
if [[ "$(systemctl show qq-math-bot -p User --value)" != qqbot ]]; then
  echo '服务运行账户与此修复脚本预期的 qqbot 不同，尚未修改程序。'
  exit 1
fi
cd "$APP_DIR"
systemctl stop qq-math-bot
restart_service() {
  local code=$?
  trap - EXIT
  if ! systemctl start qq-math-bot; then
    echo '服务启动失败，请查看 sudo journalctl -u qq-math-bot -n 100 --no-pager'
    exit 1
  fi
  if [[ $code -ne 0 ]]; then
    echo '重新排版未完成，机器人服务已请求启动；请保留上方错误信息。'
  fi
  exit "$code"
}
trap restart_service EXIT
"$APP_DIR/.venv/bin/python" - "$APP_DIR/render.py" <<'PATCH_PY'
from pathlib import Path
import ast
import shutil
import sys

path = Path(sys.argv[1])
source = path.read_text(encoding="utf-8")
marker = "Rightarrow Leftarrow leftrightarrow Leftrightarrow"
if marker not in source:
    raise SystemExit("排版文件版本不同，未作自动修改。请保留此输出。")
commands = ("Longrightarrow", "Longleftarrow", "Longleftrightarrow", "longleftrightarrow", "mid", "bigoplus")
assignment = next(node for node in ast.parse(source).body if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == "_MATH_COMMANDS" for target in node.targets))
supported = {word for node in ast.walk(assignment.value) if isinstance(node, ast.Constant)
             and isinstance(node.value, str) for word in node.value.split()}
missing = [command for command in commands if command not in supported]
patched = source
if missing:
    patched = source.replace(marker, marker + " " + " ".join(missing), 1)

font_marker = r'\usepackage{{amsmath,amssymb,mathtools,bm}}'
font_patch = r'\xeCJKDeclareCharClass{{CJK}}{{"2460->"2473}}'
if font_marker in patched and font_patch not in patched:
    patched = patched.replace(font_marker, font_marker + "\n" + font_patch, 1)
patches = [(path, source, patched)]

service_path = path.with_name("render_service.py")
if service_path.is_file():
    service = service_path.read_text(encoding="utf-8")
    service_patched = service
    if 'error_path = output_dir / "render-error.txt"' not in service:
        replacements = (
            ('    request = output_dir / "render-request.json"\n',
             '    error_path = output_dir / "render-error.txt"\n    succeeded = False\n    request = output_dir / "render-request.json"\n'),
            ('        result["pdf_path"] = str(final_pdf)\n        return result\n',
             '        result["pdf_path"] = str(final_pdf)\n        error_path.unlink(missing_ok=True)\n        succeeded = True\n        return result\n'),
            ('        raise RenderError(str(exc)) from exc\n    finally:\n        request.unlink(missing_ok=True)\n        for suffix in ("aux", "log", "out", "tex"):',
             '        error_path.write_text(str(exc)[:2000], encoding="utf-8")\n        raise RenderError(str(exc)) from exc\n    finally:\n        request.unlink(missing_ok=True)\n        for suffix in (("aux", "log", "out", "tex") if succeeded else ("aux", "out")):'),
        )
        for old, new in replacements:
            if old not in service_patched:
                raise SystemExit("render_service.py 版本不同，尚未修改程序。请保留此输出。")
            service_patched = service_patched.replace(old, new, 1)
    patches.append((service_path, service, service_patched))

# Validate every pending change before modifying either application file.
for target, original, updated in patches:
    compile(updated, str(target), "exec")
for target, original, updated in patches:
    if original == updated:
        continue
    backup = target.with_name(target.name + ".before-math-command-fix")
    if not backup.exists():
        shutil.copy2(target, backup)
    with target.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(updated)
    print("已修复排版，原文件备份：", backup)
print("标准箭头、mid、bigoplus 及排版诊断已就绪；可重复运行。")
PATCH_PY
runuser -u qqbot -- env PYTHONDONTWRITEBYTECODE=1 "$APP_DIR/.venv/bin/python" - "$@" "$APP_DIR/data" <<'RENDER_PY'
import asyncio
from pathlib import Path
import sys

from render_service import render_artifacts_async
from task_store import TaskStore

store = TaskStore(Path(sys.argv[-1]))


def repair(tid):
    task = store.get(tid)
    if not task or not task["result"].get("solution"):
        raise SystemExit("未找到这道题保存的完整解答；没有调用模型或重新解题。")
    if task["state"] not in ("completed", "needs_input"):
        raise SystemExit("这道题尚未完成解答，未尝试重新排版。")
    result = task["result"]
    if result.get("pdf_path") and Path(result["pdf_path"]).is_file():
        print("已有 PDF，保留现有文件：", result["pdf_path"])
    else:
        artifacts = asyncio.run(render_artifacts_async(result["solution"], store.task_dir(tid), tid))
        result.update(artifacts)
        result.pop("notes", None)
        if artifacts.get("plot_warnings"):
            result["notes"] = "；".join(artifacts["plot_warnings"])
        store.update(tid, result=result, delivery="awaiting_query")
        print("PDF 已生成：", artifacts["pdf_path"])
    print("没有调用模型。服务重启后，请在 QQ 中发送题号领取：", tid)


failed = []
try:
    for tid in sys.argv[1:-1]:
        try:
            repair(tid)
        except (Exception, SystemExit) as exc:
            print(f"{tid} 重新排版未完成：{exc}", file=sys.stderr)
            failed.append(tid)
finally:
    store.close()
if failed:
    raise SystemExit("未完成的题号：" + "、".join(failed))
RENDER_PY
