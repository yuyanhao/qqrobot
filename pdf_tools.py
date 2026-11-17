"""把输入 PDF 的指定页渲染成 PNG，供视觉模型识题。"""

from __future__ import annotations

import io
import math
import os
import re
import threading
import unicodedata
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

PDFIUM_LOCK = threading.RLock()
MAX_PAGE_PIXELS = 6_000_000
MAX_BATCH_PIXELS = 18_000_000
MAX_SIDE_PIXELS = 3600
MAX_PNG_BYTES = 4_500_000
MAX_PDF_BYTES = 50 * 1024 * 1024
MAX_INPUT_PDF_MB = 10


def _env_page_limit(name, default, low, high):
    try:
        value = int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


# Per-PDF and whole-task budgets. The worker process is launched with
# PDF_INPUT_MAX_PAGES passed through, so both sides enforce the same limit.
MAX_INPUT_PAGES = _env_page_limit("PDF_INPUT_MAX_PAGES", 10, 1, 50)
MAX_TASK_INPUT_PAGES = _env_page_limit("PDF_TASK_MAX_PAGES", 20, 1, 200)
SECONDARY_INPUT_PAGES = min(5, MAX_INPUT_PAGES)
_PAGE_CANDIDATE_RE = re.compile(r"(?<![\d.])(?:第\s*)?[+-]?\d[\d\s.第页+\-–—~～至到、,和及/]*页")
_PAGE_NUMBER = r"(?:第\s*)?[+-]?\d+"
_PAGE_RANGE = rf"{_PAGE_NUMBER}\s*(?:页\s*)?(?:[-–—~～至到]\s*{_PAGE_NUMBER}\s*(?:页\s*)?)?"
_PAGE_SPEC_RE = re.compile(rf"{_PAGE_RANGE}(?:[、,和及]\s*{_PAGE_RANGE})*")
_PAGE_COUNT_RE = re.compile(r"(?:总共|一共|共有|合计|总计|共|总页数(?:为|是|:)?|有)\s*\d+\s*页")
_CHINESE_PAGE_RE = re.compile(r"第\s*[零〇一二三四五六七八九十百千万两]+\s*页")
_READ_SPEC_RE = re.compile(
    r"(?:只读|仅读|只看|仅看|读取|阅读)\s*(?P<spec>(?:第\s*)?[+-]?\d[\d\s.第页+\-–—~～至到、,和及/]*)")
_LEADING_PAGE_RE = re.compile(
    r"(?:(?:只读|仅读|只看|仅看)\s*)?(?:第\s*)?[+\-＋－]?\d[\d\s.．第页+\-＋－–—~～至到、,，和及/]*页")


class PDFError(RuntimeError):
    """PDF 下载后无法打开/渲染。"""


def _page_number(value):
    if len(value.lstrip("+-")) > 10:
        raise PDFError("页码数值过大，请核对后重新指定。")
    try:
        return int(value)
    except ValueError:
        raise PDFError("页码格式无效，请写“第3页”或“第3-5页”。") from None


def pdf_page_count(path: Path):
    with PDFIUM_LOCK:
        try:
            with pdfium.PdfDocument(path) as document:
                pages = len(document)
        except Exception as exc:
            raise PDFError("PDF 打不开（可能加密或文件损坏）。") from exc
    if not pages:
        raise PDFError("PDF 没有页面。")
    return pages


def explicit_pdf_selection(text):
    text = unicodedata.normalize("NFKC", text or "").strip()
    spec = re.sub(r"^(?:只读|仅读|只看|仅看)\s*", "", text)
    if spec.endswith("页") and _PAGE_SPEC_RE.fullmatch(spec):
        _validate_selection_page_count(spec)
        return spec
    if spec != text or _PAGE_CANDIDATE_RE.fullmatch(spec) or _CHINESE_PAGE_RE.fullmatch(spec):
        raise PDFError("页码格式无效，请写“第3页”或“第3-5页”。")
    return ""


def _selection_pages(spec):
    requested = set()
    for part in re.split(r"[、,和及]", spec):
        endpoints = re.fullmatch(
            r"\s*([+-]?\d+)\s*(?:页\s*)?(?:[-–—~～至到]\s*([+-]?\d+)\s*(?:页\s*)?)?\s*",
            part.replace("第", "").replace("页", ""))
        if not endpoints:
            raise PDFError("页码格式无效，请写“第3页”或“第3-5页”。")
        start = _page_number(endpoints.group(1))
        end = _page_number(endpoints.group(2) or str(start))
        if start < 1 or end < 1:
            raise PDFError("页码必须是正整数，请写“第3页”或“第3-5页”。")
        if abs(end - start) + 1 > MAX_INPUT_PAGES:
            raise PDFError(f"单份 PDF 每次最多读取 {MAX_INPUT_PAGES} 页，请分段指定页码。")
        requested.update(range(min(start, end), max(start, end) + 1))
        if len(requested) > MAX_INPUT_PAGES:
            raise PDFError(f"单份 PDF 每次最多读取 {MAX_INPUT_PAGES} 页，请分段指定页码。")
    return requested


def _validate_selection_page_count(spec):
    return len(_selection_pages(spec))


def pdf_selection_page_count(spec):
    """Count unique pages in an already-validated explicit selection."""
    if not spec:
        return 0
    return _validate_selection_page_count(spec)


def pdf_pages_from_text(text):
    """Return page numbers mentioned anywhere in a message, without a PDF page count."""
    text = unicodedata.normalize("NFKC", text or "")
    if _CHINESE_PAGE_RE.search(text):
        raise PDFError("请用阿拉伯数字指定页码，例如“第3页”或“第3-5页”")
    text = _PAGE_COUNT_RE.sub(" ", text)
    for match in _READ_SPEC_RE.finditer(text):
        specification = match.group("spec").strip(" ,、")
        if text[match.end():].startswith(("题", "问")):
            continue
        if not specification.endswith("页"):
            raise PDFError("页码格式无效，请写“第3页”“第3-5页”或“第2、5、8-10页”")
    pages = set()
    for match in _PAGE_CANDIDATE_RE.finditer(text):
        specification = match.group()
        if not _PAGE_SPEC_RE.fullmatch(specification):
            raise PDFError("页码格式无效，请写“第3页”“第3-5页”或“第2、5、8-10页”")
        pages.update(_selection_pages(specification))
        if len(pages) > MAX_INPUT_PAGES:
            raise PDFError(f"单份 PDF 每次最多读取 {MAX_INPUT_PAGES} 页，请分段指定页码。")
    return pages


def split_pdf_selection(text):
    """Read a leading page selection and keep the following draft verbatim."""
    text = text.strip()
    match = _LEADING_PAGE_RE.match(text)
    if match:
        selection = explicit_pdf_selection(match[0])
        return selection, text[match.end():].lstrip(" \t\r\n　,，。:：")
    if _CHINESE_PAGE_RE.match(text):
        raise PDFError("请用阿拉伯数字指定页码，例如“第3页”或“第3-5页”")
    selection = explicit_pdf_selection(text)
    return selection, "" if selection else text


def _select_pages(text: str, total: int, max_pages: int = 0,
                  auto_pages: int = 0) -> tuple[list[int], bool]:
    """Select physical PDF pages; explicit selections win over automatic mode."""
    requested: set[int] = set()
    text = unicodedata.normalize("NFKC", text or "")
    if _CHINESE_PAGE_RE.search(text):
        raise PDFError("请用阿拉伯数字指定页码，例如“第3页”或“第3-5页”")
    # Page counts describe a document, rather than adding pages to the selection.
    text = _PAGE_COUNT_RE.sub(" ", text)
    for match in _READ_SPEC_RE.finditer(text):
        specification = match.group("spec").strip(" ,、")
        if text[match.end():].startswith(("题", "问")):
            continue
        if not specification.endswith("页"):
            raise PDFError("页码格式无效，请写“第3页”“第3-5页”或“第2、5、8-10页”")
    selected = False
    for match in _PAGE_CANDIDATE_RE.finditer(text):
        specification = match.group()
        if not _PAGE_SPEC_RE.fullmatch(specification):
            raise PDFError("页码格式无效，请写“第3页”“第3-5页”或“第2、5、8-10页”")
        selected = True
        for part in re.split(r"[、,和及]", specification):
            endpoints = re.fullmatch(
                r"\s*([+-]?\d+)\s*(?:[-–—~～至到]\s*([+-]?\d+))?\s*",
                part.replace("第", "").replace("页", ""))
            if not endpoints:
                raise PDFError("页码格式无效，请写“第3页”或“第3-5页”。")
            start = _page_number(endpoints.group(1))
            end = _page_number(endpoints.group(2) or str(start))
            if not (1 <= start <= total and 1 <= end <= total):
                raise PDFError(f"PDF 共 {total} 页，页码必须在 1-{total} 之间；未读取部分页码，请重新指定")
            if abs(end - start) + 1 > MAX_INPUT_PAGES:
                raise PDFError(f"单份 PDF 每次最多读取 {MAX_INPUT_PAGES} 页，请分段指定页码。")
            requested.update(range(min(start, end), max(start, end) + 1))
            if len(requested) > MAX_INPUT_PAGES:
                raise PDFError(f"单份 PDF 每次最多读取 {MAX_INPUT_PAGES} 页，请分段指定页码。")
    if not selected:
        if int(auto_pages or 0) > 0:
            count = min(total, int(auto_pages), MAX_INPUT_PAGES)
            if int(max_pages) > 0:
                count = min(count, int(max_pages))
            return list(range(1, count + 1)), False
        raise PDFError(f"PDF 共 {total} 页，必须明确指定要读取的页码；每份 PDF 每次最多读取 {MAX_INPUT_PAGES} 页。示例：“第3页”或“第3-5页”。")
    pages = sorted(requested)
    limit = min(MAX_INPUT_PAGES, int(max_pages)) if int(max_pages) > 0 else MAX_INPUT_PAGES
    if len(pages) > limit:
        raise PDFError(f"本次指定 {len(pages)} 页，超过每份 PDF 最多读取 {limit} 页的限制；请分段发送。")
    return pages, selected


def png_bytes(image: Image.Image, max_bytes: int = MAX_PNG_BYTES) -> bytes:
    """Encode one page; reduce pixels repeatedly until the actual byte cap holds."""
    current = image.copy()
    try:
        for _ in range(12):
            buffer = io.BytesIO()
            current.save(buffer, format="PNG", optimize=True)
            data = buffer.getvalue()
            if len(data) <= max_bytes:
                return data
            width, height = current.size
            if min(width, height) < 128:
                break
            smaller = current.resize((max(1, int(width * .75)), max(1, int(height * .75))), Image.Resampling.LANCZOS)
            current.close()
            current = smaller
        raise PDFError("页面图片压缩后仍超过发送大小限制")
    finally:
        current.close()


def safe_page_scale(page, dpi: int, pixel_budget: int) -> float:
    width, height = page.get_size()
    if not all(math.isfinite(value) and 0 < value <= 14400 for value in (width, height)):
        raise PDFError("PDF 页面尺寸异常")
    scale = min(dpi / 72, math.sqrt(pixel_budget / (width * height)), MAX_SIDE_PIXELS / width, MAX_SIDE_PIXELS / height)
    # PDFium rounds each side up; account for those extra pixels before allocating.
    while math.ceil(width * scale) * math.ceil(height * scale) > pixel_budget:
        scale *= .999
    return scale


def _render_pages(document, numbers: list[int], dpi: int) -> list[bytes]:
    pixel_budget = min(MAX_PAGE_PIXELS, MAX_BATCH_PIXELS // max(1, min(4, len(numbers))))
    images = []
    for number in numbers:
        page = document[number - 1]
        try:
            bitmap = page.render(scale=safe_page_scale(page, dpi, pixel_budget))
            try:
                image = bitmap.to_pil().convert("RGB")
            finally:
                bitmap.close()
            try:
                images.append(png_bytes(image))
            finally:
                image.close()
        except PDFError:
            raise
        except Exception as exc:
            raise PDFError(f"PDF 第 {number} 页渲染失败") from exc
        finally:
            page.close()
    return images


def render_pdf(
    data: bytes,
    text: str = "",
    max_pages: int = 0,
    dpi: int = 160,
    auto_pages: int = 0,
) -> tuple[list[bytes], int, list[int], bool]:
    """Render selected pages. Large inputs should use render_pdf_batch instead."""
    images, total, pages, selected, _ = _render_input_pdf(data, text, max_pages, dpi, auto_pages=auto_pages)
    return images, total, pages, selected


def render_pdf_batch(path: Path, text: str = "", max_pages: int = 0, dpi: int = 160,
                     offset: int = 0, count: int = 4, *, book=False,
                     auto_pages: int = 0) -> tuple[list[bytes], int, list[int], bool, int]:
    """Render at most four selected pages and report the full selection count."""
    if int(offset) < 0 or not 1 <= int(count) <= 4:
        raise PDFError("输入页码批次无效")
    return _render_input_pdf(path, text, max_pages, dpi, int(offset), int(count),
                             book=book, auto_pages=auto_pages)


def _render_input_pdf(source: bytes | Path, text: str, max_pages: int, dpi: int,
                      offset: int = 0, count: int | None = None, *, book=False,
                      auto_pages: int = 0):
    size = source.stat().st_size if isinstance(source, Path) else len(source)
    if not size:
        raise PDFError("PDF 为空")
    if not book and size > MAX_INPUT_PDF_MB * 1024**2:
        raise PDFError(f"PDF 超过 {MAX_INPUT_PDF_MB} MB")
    with PDFIUM_LOCK:
        try:
            document = pdfium.PdfDocument(str(source) if isinstance(source, Path) else source)
        except Exception as exc:
            raise PDFError("PDF 打不开（可能加密或文件损坏）") from exc
        try:
            total = len(document)
            if total <= 0:
                raise PDFError("PDF 没有页面")
            pages, selected = _select_pages(
                text, total, max_pages, auto_pages=0 if book else auto_pages)
            if book and not selected:
                raise PDFError("引用书籍时请指定页码。")
            selection_count = len(pages)
            pages = pages[offset:] if count is None else pages[offset:offset + count]
            images = _render_pages(document, pages, max(100, min(int(dpi), 240)))
            return images, total, pages, selected, selection_count
        finally:
            document.close()


def render_pdf_pages(path: Path, start_page: int = 1, count: int = 2, dpi: int = 160, max_document_pages: int | None = None) -> tuple[list[bytes], int, list[int]]:
    """Render a bounded preview batch while preserving the original document."""
    if not 1 <= int(count) <= 4 or int(start_page) < 1:
        raise PDFError("预览页码或数量无效")
    if path.stat().st_size > MAX_PDF_BYTES:
        raise PDFError("PDF 超过 50 MB")
    with PDFIUM_LOCK:
        try:
            document = pdfium.PdfDocument(str(path))
        except Exception as exc:
            raise PDFError("PDF 打不开") from exc
        try:
            total = len(document)
            if total < 1:
                raise PDFError("PDF 没有页面")
            if max_document_pages is not None and total > max_document_pages:
                raise PDFError(f"完整解答共 {total} 页，超过 {max_document_pages} 页的资源上限；请拆成几个问题，未截断交付")
            if start_page > total:
                raise PDFError(f"PDF 共 {total} 页，没有第 {start_page} 页")
            numbers = list(range(start_page, min(total, start_page + count - 1) + 1))
            return _render_pages(document, numbers, max(100, min(int(dpi), 240))), total, numbers
        finally:
            document.close()
