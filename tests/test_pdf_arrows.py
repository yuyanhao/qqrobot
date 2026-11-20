import asyncio
import json
from pathlib import Path
import shutil

import pytest

from qqrobot.rendering.render import sanitize_math
from qqrobot.rendering.render_service import render_artifacts_async


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
