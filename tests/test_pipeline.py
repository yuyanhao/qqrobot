import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import shutil

import pytest
import pypdfium2 as pdfium

from ai import AIError
from render import RenderError
from render_service import render_artifacts_async, render_preview_async
from solver import Solver
from task_store import TaskStore

PHYSICS_PROBLEM = "一个质量为m的小球从H处释放，初速度向下v0（未知），原速率反弹后恰好与一个从H+h高度（h未知）静止释放的M在H处弹性碰撞，m再次从地面反弹后，又恰好在H高度与回落的M发生第二次碰撞，求v0和h"


def solution():
    return {"title": "测试解答", "problem_latex": "测试", "steps": [{"text": "核对题设", "math": "x=1"}],
            "answer_latex": "1", "check_latex": "", "status": "complete", "plots": [], "search_query": ""}


def test_physics_text_and_extra_reach_solver_and_verifier(monkeypatch, tmp_path):
    seen = {}
    class FakeAI:
        async def plan_problem(self, problem, extra):
            return {"accepted": True, "domain": "physics", "needs_search": False, "search_query": ""}
        async def solve(self, problem, extra, *args):
            seen["solve"] = problem, extra
            return solution()
        async def verify(self, problem, result, timeout, *, extra, evidence):
            seen["verify"] = problem, extra
            raise AIError("模拟复核超时", "timeout", "复核")
    async def fake_render(doc, directory, tid):
        return {"total_pages": 6, "preview_paths": [], "plot_warnings": []}
    monkeypatch.setattr("solver.render_artifacts_async", fake_render)
    config = SimpleNamespace(sympy=False, verify=True, search=False, solve_timeout=600, verify_timeout=300)
    store = TaskStore(tmp_path)
    task, _ = store.create("u", "m", "c2c", "u", {"text": PHYSICS_PROBLEM, "extra": "先说明碰撞前后正方向", "attachments": []})
    asyncio.run(Solver(store, FakeAI(), config).run(task))
    result = store.get(task["task_id"])
    assert PHYSICS_PROBLEM in seen["solve"][0] and seen["solve"] == seen["verify"]
    assert result["state"] == "completed" and result["result"]["answer_status"] == "uncertain"
    assert "未完成" in result["result"]["verification"]
    assert Path(result["result"]["text_path"]).exists()
    store.close()


def test_text_attachment_is_in_actual_problem(monkeypatch, tmp_path):
    async def download(*args, **kwargs):
        return SimpleNamespace(data="附件原题：求定积分".encode("utf-8"))
    monkeypatch.setattr("solver.download_file", download)
    store = TaskStore(tmp_path)
    task, _ = store.create("u", "m", "c2c", "u", {"text": "请解附件", "attachments": [{"url": "https://example.com/p.txt", "name": "p.txt"}]})
    solver = Solver(store, None, SimpleNamespace())
    problem, _, _ = asyncio.run(solver.inputs(task, store.task_dir(task["task_id"])))
    assert "附件原题：求定积分" in problem
    store.close()


def test_failed_pdf_keeps_text_and_points_to_text_command(monkeypatch, tmp_path):
    class FakeAI:
        async def plan_problem(self, problem, extra):
            return {"accepted": True, "domain": "math", "needs_search": False, "search_query": ""}

        async def solve(self, *args):
            return solution()

    async def failed_render(doc, directory, tid):
        raise RenderError("synthetic compiler failure")

    monkeypatch.setattr("solver.render_artifacts_async", failed_render)
    config = SimpleNamespace(sympy=False, verify=False, search=False, solve_timeout=600, verify_timeout=300)
    store = TaskStore(tmp_path)
    try:
        task, _ = store.create("u", "m", "c2c", "u", {"text": "求积分", "attachments": []})
        asyncio.run(Solver(store, FakeAI(), config).run(task))
        row = store.get(task["task_id"])
        assert row["state"] == "completed"
        assert Path(row["result"]["text_path"]).is_file()
        assert f"/文字 {task['task_id']} 1" in row["result"]["notes"]
        assert "用题号领取文本文件" not in row["result"]["notes"]
        assert not row["result"].get("pdf_path")
    finally:
        store.close()


@pytest.mark.skipif(not shutil.which("xelatex"), reason="XeLaTeX unavailable")
def test_full_long_pdf_keeps_all_pages_and_only_three_previews(tmp_path):
    doc = solution()
    doc["steps"] = [{"text": f"第 {i+1} 个推导段。" + "这里展开条件核验与中间计算，确保长解答不会按四页截断。" * 18,
                     "math": r"\begin{pmatrix}a&b\\c&d\end{pmatrix}"} for i in range(28)]
    async def check():
        result = await render_artifacts_async(doc, tmp_path, "Q261026-000001")
        assert result["total_pages"] > 4
        assert len(result["preview_paths"]) == 3
        pdf = pdfium.PdfDocument(result["pdf_path"])
        try: assert len(pdf) == result["total_pages"]
        finally: pdf.close()
        last = await render_preview_async(Path(result["pdf_path"]), tmp_path, result["total_pages"], 2)
        assert len(last["preview_paths"]) == 1
        assert all(Path(p).stat().st_size < 4_500_000 for p in last["preview_paths"])
    asyncio.run(check())


@pytest.mark.skipif(not shutil.which("xelatex"), reason="XeLaTeX unavailable")
def test_plots_are_embedded_without_generated_code(tmp_path):
    doc = solution()
    doc["plots"] = [{"kind": "function", "title": "Function", "x_range": [-3, 3],
                     "curves": [{"expression": "sin(x)", "label": "sin(x)"}]},
                    {"kind": "projectile", "v0": 20, "angle_deg": 45, "g": 9.8, "title": "Trajectory"}]
    result = asyncio.run(render_artifacts_async(doc, tmp_path, "Q261026-000002"))
    assert not result["plot_warnings"]
    assert (tmp_path / "plot-1.png").is_file() and (tmp_path / "plot-2.png").is_file()
    assert Path(result["pdf_path"]).stat().st_size > 1000
