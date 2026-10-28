import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from render import sanitize_math
from render_service import render_artifacts_async
from task_store import TaskStore


def repair_block(marker):
    script = (Path(__file__).resolve().parent.parent / "repair_pdf.sh").read_text(encoding="utf-8")
    return script.split(f"<<'{marker}'\n", 1)[1].split(f"\n{marker}", 1)[0]


@pytest.mark.parametrize("command", ["Longleftrightarrow", "Longrightarrow", "Longleftarrow", "longleftrightarrow"])
def test_standard_long_arrows_are_preserved(command):
    formula = r"h(x)<0\quad(0<x<1)" + "\\" + command + " f(x)>P(x)."
    assert sanitize_math(formula) == formula


@pytest.mark.skipif(not shutil.which("xelatex"), reason="XeLaTeX unavailable")
def test_actual_failed_convexity_answer_compiles(tmp_path):
    path = Path(__file__).with_name("fixtures") / "convex-interpolation.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert any(r"\Longleftrightarrow" in step["math"] for step in doc["steps"])
    result = asyncio.run(render_artifacts_async(doc, tmp_path, "Q261027-000005"))
    assert Path(result["pdf_path"]).is_file()
    assert result["total_pages"] >= 1
    assert not result["plot_warnings"]


def test_repair_preserves_original_and_is_repeatable(tmp_path):
    path = tmp_path / "render.py"
    original = '_MATH_COMMANDS = "Rightarrow Leftarrow leftrightarrow Leftrightarrow mapsto".split()\n'
    path.write_text(original, encoding="utf-8")
    command = [sys.executable, "-X", "utf8", "-c", repair_block("PATCH_PY"), str(path)]
    subprocess.run(command, check=True, capture_output=True)
    patched = path.read_text(encoding="utf-8")
    subprocess.run(command, check=True, capture_output=True)
    assert path.read_text(encoding="utf-8") == patched
    assert path.with_name("render.py.before-math-command-fix").read_text(encoding="utf-8") == original
    assert patched.count("Longleftrightarrow") == 1
    assert "mid" in patched and "bigoplus" in patched


def test_repair_checks_service_version_before_modifying_renderer(tmp_path):
    path = tmp_path / "render.py"
    original = '_MATH_COMMANDS = "Rightarrow Leftarrow leftrightarrow Leftrightarrow mapsto".split()\n'
    path.write_text(original, encoding="utf-8")
    path.with_name("render_service.py").write_text("unsupported_version = True\n", encoding="utf-8")
    command = [sys.executable, "-X", "utf8", "-c", repair_block("PATCH_PY"), str(path)]
    result = subprocess.run(command, capture_output=True)
    assert result.returncode != 0
    assert path.read_text(encoding="utf-8") == original
    assert not path.with_name("render.py.before-math-command-fix").exists()


@pytest.mark.skipif(not shutil.which("xelatex"), reason="XeLaTeX unavailable")
def test_repair_renders_saved_solution_and_updates_task(tmp_path):
    doc = json.loads((Path(__file__).with_name("fixtures") / "convex-interpolation.json").read_text(encoding="utf-8"))
    store = TaskStore(tmp_path)
    task, _ = store.create("owner", "message", "group", "group", {})
    tid = task["task_id"]
    store.update(tid, state="completed", result={"solution": doc, "verification": "保留复核状态", "notes": "PDF 排版未完成"})
    store.close()
    command = [sys.executable, "-X", "utf8", "-c", repair_block("RENDER_PY"), tid, str(tmp_path)]
    subprocess.run(command, cwd=Path(__file__).resolve().parent.parent, check=True, capture_output=True)
    store = TaskStore(tmp_path)
    try:
        repaired = store.get(tid)
        assert repaired["state"] == "completed"
        assert repaired["result"]["verification"] == "保留复核状态"
        assert repaired["delivery"] == "awaiting_query"
        assert "notes" not in repaired["result"]
        pdf = Path(repaired["result"]["pdf_path"])
        assert pdf.is_file()
        before = pdf.stat().st_mtime_ns
        subprocess.run(command, cwd=Path(__file__).resolve().parent.parent, check=True, capture_output=True)
        assert pdf.stat().st_mtime_ns == before
    finally:
        store.close()


@pytest.mark.skipif(not shutil.which("xelatex"), reason="XeLaTeX unavailable")
def test_repair_handles_multiple_saved_answers_even_if_one_id_is_missing(tmp_path):
    store = TaskStore(tmp_path)
    task_ids = []
    for index, fixture in enumerate(("real-diagonalization.json", "real-centralizer.json")):
        doc = json.loads((Path(__file__).with_name("fixtures") / fixture).read_text(encoding="utf-8"))
        task, _ = store.create("owner", f"message-{index}", "group", "group", {})
        task_ids.append(task["task_id"])
        store.update(task["task_id"], state="completed", result={
            "solution": doc, "verification": "保留复核状态", "notes": "PDF 排版未完成"})
    store.close()
    command = [sys.executable, "-X", "utf8", "-c", repair_block("RENDER_PY"),
               "Q000000-000099", *task_ids, str(tmp_path)]
    result = subprocess.run(command, cwd=Path(__file__).resolve().parent.parent, capture_output=True)
    assert result.returncode != 0
    assert "Q000000-000099" in result.stderr.decode("utf-8")
    store = TaskStore(tmp_path)
    try:
        for tid in task_ids:
            task = store.get(tid)
            assert Path(task["result"]["pdf_path"]).is_file()
            assert task["result"]["verification"] == "保留复核状态"
            assert task["delivery"] == "awaiting_query"
            assert "notes" not in task["result"]
    finally:
        store.close()
