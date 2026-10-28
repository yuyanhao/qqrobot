import asyncio
import json
from pathlib import Path
import shutil

import pypdfium2 as pdfium
import pytest

from render import RenderError, sanitize_math
from render_service import render_artifacts_async


@pytest.mark.parametrize("formula", [
    r"m_A(t)\mid t^2+1",
    r"V=\bigoplus_{i=1}^r E_i",
])
def test_standard_commands_from_failed_answers_are_preserved(formula):
    assert sanitize_math(formula) == formula


@pytest.mark.skipif(not shutil.which("xelatex"), reason="XeLaTeX unavailable")
@pytest.mark.parametrize("fixture", ["real-diagonalization.json", "real-centralizer.json"])
def test_actual_failed_algebra_answers_compile_with_all_question_labels(tmp_path, fixture):
    doc = json.loads((Path(__file__).with_name("fixtures") / fixture).read_text(encoding="utf-8"))
    result = asyncio.run(render_artifacts_async(doc, tmp_path, "Q261027-000011"))
    assert result["total_pages"] >= 2
    assert len(result["preview_paths"]) == 2
    assert not result["plot_warnings"]
    pdf = pdfium.PdfDocument(result["pdf_path"])
    text = []
    try:
        for index in range(len(pdf)):
            page = pdf[index]
            textpage = page.get_textpage()
            try:
                text.append(textpage.get_text_range())
            finally:
                textpage.close()
                page.close()
    finally:
        pdf.close()
    if fixture == "real-diagonalization.json":
        assert "①" in "".join(text) and "②" in "".join(text)
    assert not (tmp_path / "render-error.txt").exists()
    assert not (tmp_path / "solution.tex").exists()
    assert not (tmp_path / "solution.log").exists()


def test_failed_render_retains_diagnostics_without_partial_pdf(monkeypatch, tmp_path):
    async def fail_worker(script, directory, args, *, timeout):
        (directory / "solution.tex").write_text("synthetic invalid TeX", encoding="utf-8")
        (directory / "solution.log").write_text("synthetic compiler error", encoding="utf-8")
        (directory / "solution.aux").write_text("partial aux", encoding="utf-8")
        (directory / "solution.pdf").write_bytes(b"partial PDF")
        raise RuntimeError("synthetic render failure")

    monkeypatch.setattr("render_service.run_worker", fail_worker)
    with pytest.raises(RenderError, match="synthetic render failure"):
        asyncio.run(render_artifacts_async({}, tmp_path, "Q261027-000012"))
    assert (tmp_path / "solution.tex").read_text(encoding="utf-8") == "synthetic invalid TeX"
    assert "compiler error" in (tmp_path / "solution.log").read_text(encoding="utf-8")
    assert "synthetic render failure" in (tmp_path / "render-error.txt").read_text(encoding="utf-8")
    assert not list(tmp_path.glob("*.pdf"))
    assert not (tmp_path / "solution.aux").exists()
    assert not (tmp_path / "render-request.json").exists()
