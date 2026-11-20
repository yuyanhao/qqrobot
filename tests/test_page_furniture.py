"""Page furniture copied by the vision model, and per-formula degradation."""
import shutil

import pypdfium2 as pdfium
import pytest

from qqrobot.rendering.render import RenderError, build_tex, sanitize_math, strip_page_furniture


def test_footer_block_is_dropped_from_the_transcribed_problem():
    text = ("求通解 $y=xy'$。\n"
            "\\begin{flushright} \\textbullet\\ 42\\ \\textbullet \\end{flushright}")
    assert strip_page_furniture(text) == "求通解 $y=xy'$。"


@pytest.mark.parametrize("furniture", ["第 42 页", "42", "• 42 •", "Page 42 of 222", "- 42 -"])
def test_standalone_page_numbers_are_dropped(furniture):
    assert strip_page_furniture(f"{furniture}\n$y=xy'$") == "$y=xy'$"


def test_prose_referring_to_a_page_is_preserved():
    text = "解得 $x=1$，见第 42 页的定理。"
    assert strip_page_furniture(text) == text


def test_centered_content_is_preserved():
    text = "\\begin{center}第一章 基本概念\\end{center}\n题目正文"
    assert strip_page_furniture(text) == text


def test_exercise_numbers_are_preserved():
    text = "42. 求 $y'=2x$ 的通解。"
    assert strip_page_furniture(text) == text


def test_textbullet_is_accepted_as_a_math_symbol():
    assert sanitize_math(r"3\textbullet 4") == r"3\textbullet 4"


def test_one_unsupported_formula_degrades_without_failing_the_document():
    doc = {"title": "降级", "problem_latex": "求 $y=2x$；此处 $\\foo{x}$ 不受支持。",
           "steps": [{"text": "步骤", "math": ""}], "answer_latex": "$y=x^2+C$", "check_latex": ""}
    notes = []
    tex = build_tex(doc, notes=notes)
    assert notes == ["公式含不支持的命令：foo"]
    assert "\\(y=2x\\)" in tex and "\\(y=x^2+C\\)" in tex
    # Only the rejected formula keeps its source text, escaped so it still compiles.
    assert "\\textbackslash{}foo" in tex
    assert tex.endswith(r"\end{document}")


def test_strict_build_still_rejects_unsupported_commands():
    doc = {"title": "严格", "problem_latex": "$\\foo{x}$", "steps": [],
           "answer_latex": "", "check_latex": ""}
    with pytest.raises(RenderError, match="foo"):
        build_tex(doc)


@pytest.mark.skipif(not shutil.which("xelatex"), reason="XeLaTeX unavailable")
def test_rejected_formula_degrades_instead_of_dumping_the_whole_document(tmp_path, monkeypatch):
    from qqrobot.rendering.render_worker import render_document

    monkeypatch.chdir(tmp_path)
    doc = {"title": "降级", "problem_latex": "求 $y=2x$；一处 $\\foo{x}$ 不受支持；页脚符号 $3\\textbullet 4$。",
           "steps": [{"text": "先积分。", "math": "y=\\int 2x\\,dx=x^2+C"}],
           "answer_latex": "$y=x^2+C$", "check_latex": "", "plots": []}
    warnings = render_document({"mode": "solution", "doc": doc}, "solution", tmp_path)
    assert warnings == ["1 处公式无法排版，已按原文显示；其余内容排版正常。"]
    pdf = pdfium.PdfDocument(str(tmp_path / "solution.pdf"))
    try:
        text = "".join(pdf[index].get_textpage().get_text_range() for index in range(len(pdf)))
    finally:
        pdf.close()
    assert "本文件保留了无法排版的公式原文" not in text
    assert "\u2022" in text
    assert "由 AI 生成，仅供参考" in text
