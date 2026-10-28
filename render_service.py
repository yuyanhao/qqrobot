"""Retain complete PDFs and render only a bounded preview batch."""
from __future__ import annotations

import json
from contextlib import aclosing
from pathlib import Path
import re

from process_tools import run_worker
from pdf_tools import PDFError
from render import RenderError

WORKER = Path(__file__).with_name("render_worker.py")


async def render_artifacts_async(doc: dict, output_dir: Path, task_id: str,
                                 preview_pages: int = 3, timeout: int = 120) -> dict:
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if not re.fullmatch(r"[A-Za-z0-9-]{1,40}", task_id):
        raise RenderError("题号格式无效")
    if len(json.dumps(doc, ensure_ascii=False)) > 120000:
        raise RenderError("解答超过排版资源上限，请拆题")
    error_path = output_dir / "render-error.txt"
    succeeded = False
    request = output_dir / "render-request.json"
    display_doc = {**doc, "title": f"{doc.get('title', '解答')} · {task_id}"}
    request.write_text(json.dumps({"mode": "solution", "doc": display_doc,
                                  "count": max(1, min(preview_pages, 3))}, ensure_ascii=False), encoding="utf-8")
    try:
        result = json.loads(await run_worker(WORKER, output_dir, (request.name,), timeout=timeout))
        final_pdf = output_dir / f"{task_id}.pdf"
        Path(result["pdf_path"]).replace(final_pdf)
        result["pdf_path"] = str(final_pdf)
        error_path.unlink(missing_ok=True)
        succeeded = True
        return result
    except Exception as exc:
        for path in output_dir.glob("*.pdf"):
            path.unlink(missing_ok=True)
        error_path.write_text(str(exc)[:2000], encoding="utf-8")
        raise RenderError(str(exc)) from exc
    finally:
        request.unlink(missing_ok=True)
        for suffix in (("aux", "log", "out", "tex") if succeeded else ("aux", "out")):
            (output_dir / f"solution.{suffix}").unlink(missing_ok=True)


async def render_preview_async(pdf_path: Path, output_dir: Path, start_page: int = 1,
                               count: int = 2, timeout: int = 60) -> dict:
    if not 1 <= count <= 3 or start_page < 1:
        raise RenderError("图片页码或数量无效")
    output_dir, pdf_path = output_dir.resolve(), pdf_path.resolve()
    if pdf_path.parent != output_dir:
        raise RenderError("结果目录不匹配")
    request = output_dir / "preview-request.json"
    request.write_text(json.dumps({"mode": "preview", "pdf": pdf_path.name,
                                  "start_page": start_page, "count": count}), encoding="utf-8")
    try:
        return json.loads(await run_worker(WORKER, output_dir, (request.name,), timeout=timeout))
    except Exception as exc:
        raise RenderError(str(exc)) from exc
    finally:
        request.unlink(missing_ok=True)


async def render_input_pdf_async(data: bytes, directory: Path, text: str = "",
                                  max_pages: int = 0, dpi: int = 160, timeout: int = 60):
    """Compatibility helper; the solver consumes batches without retaining all images."""
    images, numbers = [], []
    async with aclosing(render_input_pdf_batches_async(data, directory, text, max_pages, dpi, timeout)) as batches:
        async for batch, total, pages, selected in batches:
            images.extend(batch)
            numbers.extend(pages)
    return images, total, numbers, selected


async def render_input_pdf_batches_async(data: bytes, directory: Path, text: str = "",
                                         max_pages: int = 0, dpi: int = 160, timeout: int = 60):
    """Keep one input PDF on disk and yield at most four page images at a time."""
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    pdf, request = directory / "input.pdf", directory / "input-request.json"
    try:
        pdf.write_bytes(data)
        offset = 0
        while True:
            request.write_text(json.dumps({"mode": "input", "text": text,
                                          "max_pages": max_pages, "dpi": dpi,
                                          "offset": offset, "count": 4}), encoding="utf-8")
            try:
                result = json.loads(await run_worker(WORKER, directory, (request.name,), timeout=timeout))
            except TimeoutError as exc:
                raise PDFError("PDF 页面渲染超时，请指定较少页码后重试；本次未完整读取") from exc
            except RuntimeError as exc:
                detail = str(exc).split("PDFError:", 1)[-1].strip()
                raise PDFError(detail) from exc
            paths = [Path(p) for p in result["preview_paths"]]
            try:
                images = [path.read_bytes() for path in paths]
            finally:
                for path in paths:
                    path.unlink(missing_ok=True)
            if not images:
                raise PDFError("PDF 没有读到所需页面，请重新指定页码")
            offset += len(images)
            yield images, result["total_pages"], result["page_numbers"], result["selected"]
            del images
            if offset >= result["selection_count"]:
                break
    finally:
        request.unlink(missing_ok=True)
        pdf.unlink(missing_ok=True)
        for path in directory.glob("preview-*.png"):
            path.unlink(missing_ok=True)
