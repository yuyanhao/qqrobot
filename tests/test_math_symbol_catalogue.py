"""Cover the supported symbol catalogue instead of only previously failed names."""
import asyncio
import json
from pathlib import Path
import shutil

import pytest

from render import _MATH_COMMANDS, RenderError, sanitize_math
from render_service import render_artifacts_async

DATA = json.loads((Path(__file__).with_name("fixtures") / "standard-math-symbols.json").read_text(encoding="utf-8"))
CATALOGUE = DATA["commands"]


def test_all_declared_standard_symbols_are_supported():
    expected = set(CATALOGUE) | set(DATA["extra_examples"])
    assert expected <= _MATH_COMMANDS, sorted(expected - _MATH_COMMANDS)


@pytest.mark.skipif(not shutil.which("xelatex"), reason="XeLaTeX unavailable")
def test_every_catalogued_symbol_compiles_with_the_actual_preloaded_packages(tmp_path):
    formulas = ["\\" + name + ("{x}" if kind == "accent" else "") for name, kind in CATALOGUE.items()]
    names = list(CATALOGUE) + list(DATA["extra_examples"])
    formulas += list(DATA["extra_examples"].values())
    doc = {"title": "标准数学符号覆盖检查", "problem_latex": "逐项检查已加载宏包的符号。",
           "steps": [{"text": "、".join(names[i:i + 6]), "math": r"\quad".join(formulas[i:i + 6])}
                     for i in range(0, len(formulas), 6)],
           "answer_latex": "符号覆盖检查完成。", "check_latex": "", "plots": []}
    result = asyncio.run(render_artifacts_async(doc, tmp_path, "MATH-SYMBOL-CHECK"))
    assert result["total_pages"] > 1
    assert Path(result["pdf_path"]).is_file()
    assert not (tmp_path / "render-error.txt").exists()


@pytest.mark.parametrize("command", ["input", "include", "write", "write18", "openin", "openout", "read",
                                    "def", "edef", "newcommand", "renewcommand", "csname", "catcode", "usepackage",
                                    "includegraphics", "special", "directlua"])
def test_catalogue_does_not_open_file_or_macro_interfaces(command):
    with pytest.raises(RenderError):
        sanitize_math("\\" + command + "{arbitrary}")
