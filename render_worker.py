"""Render only synthetic TeX and validated plots inside a dedicated work directory."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys

from process_tools import set_worker_limits
set_worker_limits()

from pdf_tools import render_pdf_batch, render_pdf_pages
from render import RenderError, build_favorites_tex, build_tex


def compile_tex(tex, binary):
    Path("solution.tex").write_text(tex, encoding="utf-8")
    proc = subprocess.run([binary, "-no-shell-escape", "-interaction=nonstopmode",
                           "-halt-on-error", "-file-line-error", "solution.tex"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          timeout=100, env={**os.environ, "openin_any": "p", "openout_any": "p"})
    if proc.returncode:
        raise RenderError("XeLaTeX 拒绝了本次排版")


def render_document(request, mode, directory):
    binary = shutil.which("xelatex")
    if not binary:
        raise ValueError("找不到 XeLaTeX")
    warnings = []
    if mode == "favorites":
        def render(plain):
            return build_favorites_tex(request["title"], request["entries"], plain=plain)
    else:
        from plot_tools import create_plots
        doc = request["doc"]
        plots, warnings = create_plots(doc.get("plots", []), directory) if doc.get("plots") else ([], [])
        figures = "\n".join(r"\begin{center}\includegraphics[width=.88\linewidth]{" + p.name +
                            r"}\end{center}" for p in plots)

        def render(plain):
            return build_tex(doc, plain=plain).replace(r"\end{document}", figures + "\n" + r"\end{document}")

    try:
        compile_tex(render(False), binary)
    except RenderError:
        for suffix in ("aux", "out", "pdf"):
            Path(f"solution.{suffix}").unlink(missing_ok=True)
        fallback = render(True).replace(r"\begin{document}", r"\begin{document}"
                                        "\n本文件保留了无法排版的公式原文。\\par\n", 1)
        compile_tex(fallback, binary)
        warnings.append("部分公式无法按原格式排版，本次 PDF 保留了原文。")
    return warnings


def main():
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    mode = request["mode"]
    directory = Path.cwd()
    if mode == "input":
        images, total, numbers, selected, selection_count = render_pdf_batch(
            Path("input.pdf"), request.get("text", ""), request.get("max_pages", 0),
            request.get("dpi", 160), request.get("offset", 0), request.get("count", 4),
            book=request.get("book", False), auto_pages=request.get("auto_pages", 0))
        result = {"total_pages": total, "page_numbers": numbers, "selected": selected,
                  "selection_count": selection_count}
    else:
        warnings = render_document(request, mode, directory) if mode in ("solution", "favorites") else []
        pdf = Path(request.get("pdf", "solution.pdf"))
        if mode == "favorites":
            print(json.dumps({"pdf_path": str(pdf.resolve()), "warnings": warnings}, ensure_ascii=False))
            return
        images, total, numbers = render_pdf_pages(pdf, request.get("start_page", 1),
                                                  request.get("count", 2), max_document_pages=40)
        result = {"pdf_path": str(pdf.resolve()), "total_pages": total, "page_numbers": numbers,
                  "plot_warnings": warnings}
    paths = []
    for number, image in zip(numbers, images):
        path = directory / f"preview-{number:03d}.png"
        path.write_bytes(image)
        paths.append(str(path))
    result["preview_paths"] = paths
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"{type(exc).__name__}: {str(exc)[:300]}", file=sys.stderr)
        sys.exit(1)
