import asyncio
import io
import re
from types import SimpleNamespace

from PIL import Image
import pypdfium2 as pdfium
import pytest

from ai import AIError
from file_tools import DownloadedFile
from pdf_tools import PDFError
from solver import InputError, Solver
from task_store import TaskStore


PDF_ATTACHMENT = {"url": "https://example.com/questions.pdf", "name": "questions.pdf"}


def png(number):
    buffer = io.BytesIO()
    with Image.new("RGB", (3, 3), (number, 0, 0)) as image:
        image.save(buffer, format="PNG")
    return buffer.getvalue()


class Recognizer:
    def __init__(self, *, fail_at=None, error=None, transcript_size=0):
        self.batches, self.user_texts = [], []
        self.fail_at, self.error, self.transcript_size = fail_at, error, transcript_size

    async def transcribe(self, images, user_text):
        numbers = []
        for data, mime in images:
            assert mime == "image/png"
            with Image.open(io.BytesIO(data)) as image:
                numbers.append(image.getpixel((0, 0))[0])
        self.batches.append(numbers)
        self.user_texts.append(user_text)
        if len(self.batches) == self.fail_at:
            raise self.error
        return {"problem_latex": "，".join(f"页面-{n}" for n in numbers) + "字" * self.transcript_size,
                "notes": "识图备注"}


def mock_download(monkeypatch, pdf_data=b"synthetic PDF"):
    calls = []

    async def download(url, **kwargs):
        calls.append(url)
        if url.endswith(".pdf"):
            return DownloadedFile(pdf_data, "questions.pdf", "application/pdf", url)
        number = int(re.search(r"page-(\d+)", url).group(1))
        return DownloadedFile(png(number), f"page-{number}.png", "image/png", url)

    monkeypatch.setattr("solver.download_file", download)
    return calls


def mock_pdf_batches(monkeypatch, numbers, *, total=None, selected=False, error=None):
    events, calls = [], []
    total = total or max(numbers)

    async def render(data, directory, text="", max_pages=0, **kwargs):
        calls.append((data, text, max_pages))
        try:
            if error:
                raise error
            for start in range(0, len(numbers), 4):
                batch = numbers[start:start + 4]
                events.append(batch)
                yield [png(n) for n in batch], total, batch, selected
        finally:
            events.append("closed")

    monkeypatch.setattr("solver.render_input_pdf_batches_async", render)
    return events, calls


def make_task(store, *, text="", collected=False, attachments=None):
    task, _ = store.create("u", "pdf", "c2c", "u", {
        "text": text, "collected": collected,
        "attachments": [PDF_ATTACHMENT] if attachments is None else attachments})
    return task


@pytest.mark.parametrize("collected", [False, True])
def test_pdf_defaults_to_all_pages_beyond_both_old_image_limits(tmp_path, monkeypatch, collected):
    async def check():
        store = TaskStore(tmp_path)
        try:
            downloads = mock_download(monkeypatch)
            expected = list(range(1, 14))
            events, calls = mock_pdf_batches(monkeypatch, expected)
            ai = Recognizer()
            task = make_task(store, collected=collected)
            # A legacy setting left on the installed server must not restore the four-page cap.
            problem, extra, notes = await Solver(store, ai, SimpleNamespace(input_pages=4)).inputs(
                task, store.task_dir(task["task_id"]))
            assert ai.batches == [[1, 2, 3, 4], [5, 6, 7, 8], [9, 10, 11, 12], [13]]
            assert list(map(int, re.findall(r"页面-(\d+)", problem))) == expected
            assert "第 9、10、11、12 页" in problem and "第 13 页" in problem
            assert "已读取全部页面" in notes and "共识别 13" in notes
            assert "识图备注" in extra
            assert len(downloads) == len(calls) == 1 and calls[0][2] == 0
            assert events[-1] == "closed"
        finally:
            store.close()
    asyncio.run(check())


def test_more_than_four_selected_pages_preserve_original_page_numbers(tmp_path, monkeypatch):
    async def check():
        store = TaskStore(tmp_path)
        try:
            mock_download(monkeypatch)
            expected = [2, 5, 8, 9, 10, 14]
            events, calls = mock_pdf_batches(monkeypatch, expected, total=20, selected=True)
            ai = Recognizer()
            task = make_task(store, text="只读第2、5、8-10、14页，解释定义")
            problem, _, notes = await Solver(store, ai, SimpleNamespace()).inputs(
                task, store.task_dir(task["task_id"]))
            assert ai.batches == [expected[:4], expected[4:]]
            assert list(map(int, re.findall(r"页面-(\d+)", problem))) == expected
            assert "第 2、5、8、9 页" in problem and "第 10、14 页" in problem
            assert "已读取指定页面" in notes and "已读取全部页面" not in notes
            assert calls[0][1] == task["request"]["text"]
            assert all(text == calls[0][1] for text in ai.user_texts)
            assert events[-1] == "closed"
        finally:
            store.close()
    asyncio.run(check())


@pytest.mark.parametrize("pdf_first", [False, True])
def test_pdf_pages_do_not_consume_other_image_slots(tmp_path, monkeypatch, pdf_first):
    async def check():
        store = TaskStore(tmp_path)
        try:
            mock_download(monkeypatch)
            mock_pdf_batches(monkeypatch, list(range(1, 14)))
            pictures = [{"url": f"https://example.com/page-{n}.png", "name": f"page-{n}.png"}
                        for n in (101, 102, 103)]
            task = make_task(store, attachments=([PDF_ATTACHMENT, *pictures] if pdf_first
                                                else [*pictures, PDF_ATTACHMENT]))
            ai = Recognizer()
            problem, _, notes = await Solver(store, ai, SimpleNamespace()).inputs(
                task, store.task_dir(task["task_id"]))
            pdf_batches = [[1, 2, 3, 4], [5, 6, 7, 8], [9, 10, 11, 12], [13]]
            expected = pdf_batches + [[101, 102, 103]] if pdf_first else [[101, 102, 103]] + pdf_batches
            assert ai.batches == expected
            assert list(map(int, re.findall(r"页面-(\d+)", problem))) == [n for batch in expected for n in batch]
            assert "共识别 16" in notes
        finally:
            store.close()
    asyncio.run(check())


@pytest.mark.parametrize("error", [PDFError("PDF 只有 13 页，没有第 20 页"),
                                 RuntimeError("工作进程失败：无效页码")])
def test_pdf_selection_or_worker_error_is_clear_input_failure(tmp_path, monkeypatch, error):
    async def check():
        store = TaskStore(tmp_path)
        try:
            mock_download(monkeypatch)
            events, _ = mock_pdf_batches(monkeypatch, [1], error=error)
            task = make_task(store, text="第20页")
            ai = Recognizer()
            with pytest.raises(InputError, match="PDF 附件 1 未完成读取") as caught:
                await Solver(store, ai, SimpleNamespace()).run(task)
            assert str(error) in str(caught.value)
            assert not ai.batches and events == ["closed"]
            current = store.get(task["task_id"])
            assert current["state"] == "reading" and not current["result"] and not current["problem"]
        finally:
            store.close()
    asyncio.run(check())


def test_later_ocr_failure_keeps_ai_classification_and_stops_pdf_reading(tmp_path, monkeypatch):
    async def check():
        store = TaskStore(tmp_path)
        try:
            mock_download(monkeypatch)
            events, _ = mock_pdf_batches(monkeypatch, list(range(1, 14)))
            error = AIError("识题超时", "timeout", "识题")
            ai = Recognizer(fail_at=2, error=error)
            task = make_task(store)
            with pytest.raises(AIError) as caught:
                await Solver(store, ai, SimpleNamespace()).run(task)
            assert caught.value is error and caught.value.code == "timeout"
            assert ai.batches == [[1, 2, 3, 4], [5, 6, 7, 8]]
            assert events == [[1, 2, 3, 4], [5, 6, 7, 8], "closed"]
            current = store.get(task["task_id"])
            assert current["state"] == "reading" and not current["result"] and not current["problem"]
        finally:
            store.close()
    asyncio.run(check())


def test_material_limit_stops_further_batches_without_partial_success(tmp_path, monkeypatch):
    async def check():
        store = TaskStore(tmp_path)
        try:
            mock_download(monkeypatch)
            events, _ = mock_pdf_batches(monkeypatch, list(range(1, 14)))
            ai = Recognizer(transcript_size=13000)
            task = make_task(store)
            with pytest.raises(InputError, match="尚未完成全文处理.*页码"):
                await Solver(store, ai, SimpleNamespace()).run(task)
            assert len(ai.batches) == 2
            assert events == [[1, 2, 3, 4], [5, 6, 7, 8], "closed"]
            current = store.get(task["task_id"])
            assert current["state"] == "reading" and not current["result"] and not current["problem"]
        finally:
            store.close()
    asyncio.run(check())


def test_real_thirteen_page_pdf_reaches_solver_in_four_batches(tmp_path, monkeypatch):
    async def check():
        store = TaskStore(tmp_path)
        try:
            document = pdfium.PdfDocument.new()
            try:
                for _ in range(13):
                    document.new_page(120, 120).close()
                output = io.BytesIO()
                document.save(output)
            finally:
                document.close()
            mock_download(monkeypatch, output.getvalue())
            task = make_task(store)
            ai = Recognizer()
            problem, _, notes = await Solver(store, ai, SimpleNamespace()).inputs(
                task, store.task_dir(task["task_id"]))
            assert list(map(len, ai.batches)) == [4, 4, 4, 1]
            assert "共 13 页，第 13 页" in problem
            assert "已读取全部页面" in notes and "共识别 13" in notes
            assert not list(store.task_dir(task["task_id"]).rglob("input.pdf"))
            assert not list(store.task_dir(task["task_id"]).rglob("preview-*.png"))
        finally:
            store.close()
    asyncio.run(check())
