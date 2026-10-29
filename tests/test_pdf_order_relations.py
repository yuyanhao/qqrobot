"""Regress the preceq whitelist failure reported for Q261029-000022."""
import asyncio
import shutil

import pytest

from render import RenderError, sanitize_math
from render_service import render_artifacts_async


@pytest.mark.parametrize("formula", [r"0\preceq A\preceq B", r"B\succeq A\succeq 0"])
def test_matrix_order_relations_are_preserved(formula):
    assert sanitize_math(formula) == formula


@pytest.mark.skipif(not shutil.which("xelatex"), reason="XeLaTeX unavailable")
def test_order_relations_reach_the_actual_pdf_renderer(tmp_path):
    doc = {"title": "矩阵偏序", "problem_latex": r"设 $0\preceq A\preceq B$。",
           "steps": [{"text": "使用半正定矩阵的偏序记号。", "math": r"B\succeq A\succeq 0"}],
           "answer_latex": r"$A\preceq B$", "check_latex": "仅验证排版符号。", "plots": []}
    result = asyncio.run(render_artifacts_async(doc, tmp_path, "Q261029-000022"))
    assert result["total_pages"] == 1
    assert len(result["preview_paths"]) == 1
    assert not (tmp_path / "render-error.txt").exists()


def test_order_relation_fix_does_not_allow_tex_file_access():
    with pytest.raises(RenderError, match="input"):
        sanitize_math(r"A\preceq B\input{secret}")
