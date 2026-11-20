import asyncio
import io
import json
import re
from types import SimpleNamespace

from PIL import Image
import pypdfium2 as pdfium
import pytest

from qqrobot.solving.ai import AIError
from qqrobot.media.file_tools import DownloadedFile
from qqrobot.media.pdf_tools import PDFError
from qqrobot.solving.solver import InputError, Solver, followup_solution_summary
from qqrobot.tasks.task_store import TaskStore


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

    async def transcribe(self, images, user_text, **options):
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

    monkeypatch.setattr("qqrobot.solving.solver.download_file", download)
    return calls


def mock_pdf_batches(monkeypatch, numbers, *, total=None, selected=False, error=None):
    events, calls = [], []
    total = total or max(numbers)

    async def render(data, directory, text="", max_pages=0, auto_pages=0, **kwargs):
        calls.append((data, text, max_pages, auto_pages))
        try:
            if error:
                raise error
            for start in range(0, len(numbers), 4):
                batch = numbers[start:start + 4]
                events.append(batch)
                yield [png(n) for n in batch], total, batch, selected
        finally:
            events.append("closed")

    monkeypatch.setattr("qqrobot.solving.solver.render_input_pdf_batches_async", render)
    return events, calls


def make_task(store, *, text="", collected=False, attachments=None):
    task, _ = store.create("u", "pdf", "c2c", "u", {
        "text": text, "collected": collected,
        "attachments": [PDF_ATTACHMENT] if attachments is None else attachments})
    return task


@pytest.mark.parametrize("collected", [False, True])
def test_pdf_reads_only_the_explicit_five_page_selection(tmp_path, monkeypatch, collected):
    async def check():
        store = TaskStore(tmp_path)
        try:
            downloads = mock_download(monkeypatch)
            expected = list(range(1, 6))
            events, calls = mock_pdf_batches(monkeypatch, expected, total=13, selected=True)
            ai = Recognizer()
            task = make_task(store, text="第1-5页", collected=collected)
            # A legacy setting may reduce the cap, but cannot make it larger.
            problem, extra, notes = await Solver(store, ai, SimpleNamespace(input_pages=4)).inputs(
                task, store.task_dir(task["task_id"]))
            assert ai.batches == [[1, 2, 3, 4], [5]]
            assert list(map(int, re.findall(r"页面-(\d+)", problem))) == expected
            assert "第 1、2、3、4 页" in problem and "第 5 页" in problem
            assert "已读取指定页面" in notes and "共识别 5" in notes
            assert "识图备注" in extra
            assert len(downloads) == len(calls) == 1 and calls[0][1] == "第1-5页"
            assert events[-1] == "closed"
        finally:
            store.close()
    asyncio.run(check())


def test_legacy_check_task_cannot_exceed_aggregate_pdf_page_limit(tmp_path, monkeypatch):
    async def check():
        store = TaskStore(tmp_path)
        try:
            question = {**PDF_ATTACHMENT, "pdf_selection": "第1-3页"}
            draft = {"url": "https://example.com/draft.pdf", "name": "draft.pdf",
                     "pdf_selection": "第4-6页"}
            task, _ = store.create("u", "legacy-check", "c2c", "u", {
                "action": "查错", "text": "我令 t=x", "question_attachments": [question],
                "attachments": [draft],
            })

            async def forbidden_download(*args, **kwargs):
                raise AssertionError("PDF validation must happen before download or OCR")

            monkeypatch.setattr("qqrobot.solving.solver.download_file", forbidden_download)
            with pytest.raises(InputError, match="超过上限 5 页"):
                await Solver(store, Recognizer(), SimpleNamespace()).run_study(
                    task, store.task_dir(task["task_id"]))
        finally:
            store.close()
    asyncio.run(check())


def test_legacy_pdf_task_without_selection_is_rejected_before_download(tmp_path, monkeypatch):
    async def check():
        store = TaskStore(tmp_path)
        try:
            task = make_task(store, text="解释这本书")

            async def forbidden_download(*args, **kwargs):
                raise AssertionError("PDF validation must happen before download or OCR")

            monkeypatch.setattr("qqrobot.solving.solver.download_file", forbidden_download)
            with pytest.raises(InputError, match="明确指定页码"):
                await Solver(store, Recognizer(), SimpleNamespace()).inputs(
                    task, store.task_dir(task["task_id"]))
        finally:
            store.close()
    asyncio.run(check())


def test_followup_solution_summary_is_bounded_and_keeps_key_steps():
    solution = {
        "title": "T" * 200,
        "status": "complete",
        "steps": [{"text": "t" * 500, "math": "m" * 1200} for _ in range(20)],
        "answer_latex": "a" * 5000,
        "check_latex": "c" * 3000,
    }
    summary = json.loads(followup_solution_summary(solution))
    assert summary["omitted_steps"] == 10
    assert len(summary["steps"]) == 10
    assert all(len(step["text"]) <= 400 and len(step["math"]) <= 1000 for step in summary["steps"])
    assert len(summary["answer_latex"]) == 4000 and len(summary["check_latex"]) == 2000


def test_solver_page_budget_validation_rejects_legacy_request_without_page_selection():
    request = {"text": "解释一下", "attachments": [PDF_ATTACHMENT]}
    with pytest.raises(InputError, match="明确指定页码"):
        Solver._validate_pdf_page_budget(request)
    request["attachments"][0] = {**PDF_ATTACHMENT, "pdf_selection": "第1-3页"}
    request["question_attachments"] = [
        {**PDF_ATTACHMENT, "pdf_selection": "第4-6页"},
    ]
    with pytest.raises(InputError, match="超过上限 5 页"):
        Solver._validate_pdf_page_budget(request)


class TruncatingRecognizer(Recognizer):
    def __init__(self, *, single_needs_budget=False):
        super().__init__()
        self.calls = []
        self.single_needs_budget = single_needs_budget

    async def transcribe(self, images, user_text, **options):
        self.calls.append((len(images), options.get("max_tokens")))
        if len(images) == 4:
            raise AIError("batch too long", "truncated", "识题")
        if len(images) == 1 and self.single_needs_budget and options.get("max_tokens") != 16000:
            raise AIError("single too long", "truncated", "识题")
        return await super().transcribe(images, user_text)


def test_truncated_ocr_batch_is_split_and_retried(tmp_path, monkeypatch):
    async def check():
        store = TaskStore(tmp_path)
        try:
            mock_download(monkeypatch)
            attachments = [{"url": f"https://example.com/page-{n}.png", "name": f"page-{n}.png"}
                           for n in (1, 2, 3, 4)]
            task = make_task(store, text="", attachments=attachments)
            ai = TruncatingRecognizer()
            blocks, _, _ = await Solver(store, ai, SimpleNamespace()).attachment_inputs(
                task, store.task_dir(task["task_id"]))
            assert ai.calls == [(4, None), (2, None), (2, None)]
            assert "拆分识别" in "".join(blocks)
        finally:
            store.close()
    asyncio.run(check())


def test_truncated_single_image_retries_with_a_larger_budget(tmp_path, monkeypatch):
    async def check():
        store = TaskStore(tmp_path)
        try:
            mock_download(monkeypatch)
            attachments = [{"url": "https://example.com/page-1.png", "name": "page-1.png"}]
            task = make_task(store, text="", attachments=attachments)
            ai = TruncatingRecognizer(single_needs_budget=True)
            await Solver(store, ai, SimpleNamespace()).attachment_inputs(
                task, store.task_dir(task["task_id"]))
            assert ai.calls == [(1, None), (1, 16000)]
        finally:
            store.close()
    asyncio.run(check())


def test_secondary_pdf_keeps_explicit_pages_without_auto_selection(tmp_path, monkeypatch):
    async def check():
        store = TaskStore(tmp_path)
        try:
            mock_download(monkeypatch)
            expected = [2, 3]
            events, calls = mock_pdf_batches(monkeypatch, expected, total=13, selected=True)
            ai = Recognizer()
            task = make_task(store, text="第2-3页 解释一下")
            blocks, ocr, notes = await Solver(store, ai, SimpleNamespace()).attachment_inputs(
                task, store.task_dir(task["task_id"]), draft=True)
            assert ai.batches == [expected]
            assert calls[0][3] == 0
            assert "已读取指定页面" in "；".join(notes)
            assert "页面-2" in "".join(blocks)
            assert events[-1] == "closed"
        finally:
            store.close()
    asyncio.run(check())


def test_more_than_four_selected_pages_preserve_original_page_numbers(tmp_path, monkeypatch):
    async def check():
        store = TaskStore(tmp_path)
        try:
            mock_download(monkeypatch)
            expected = [2, 5, 8, 9, 10]
            events, calls = mock_pdf_batches(monkeypatch, expected, total=20, selected=True)
            ai = Recognizer()
            task = make_task(store, text="只读第2、5、8-10页，解释定义")
            problem, _, notes = await Solver(store, ai, SimpleNamespace()).inputs(
                task, store.task_dir(task["task_id"]))
            assert ai.batches == [expected[:4], expected[4:]]
            assert list(map(int, re.findall(r"页面-(\d+)", problem))) == expected
            assert "第 2、5、8、9 页" in problem and "第 10 页" in problem
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
            mock_pdf_batches(monkeypatch, list(range(1, 6)), total=13, selected=True)
            pictures = [{"url": f"https://example.com/page-{n}.png", "name": f"page-{n}.png"}
                        for n in (101, 102, 103)]
            task = make_task(store, attachments=([PDF_ATTACHMENT, *pictures] if pdf_first
                                                else [*pictures, PDF_ATTACHMENT]), text="第1-5页")
            ai = Recognizer()
            problem, _, notes = await Solver(store, ai, SimpleNamespace()).inputs(
                task, store.task_dir(task["task_id"]))
            pdf_batches = [[1, 2, 3, 4], [5]]
            expected = pdf_batches + [[101, 102, 103]] if pdf_first else [[101, 102, 103]] + pdf_batches
            assert ai.batches == expected
            assert list(map(int, re.findall(r"页面-(\d+)", problem))) == [n for batch in expected for n in batch]
            assert "共识别 8" in notes
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
            events, _ = mock_pdf_batches(monkeypatch, list(range(1, 6)), total=13, selected=True)
            error = AIError("识题超时", "timeout", "识题")
            ai = Recognizer(fail_at=2, error=error)
            task = make_task(store, text="第1-5页")
            with pytest.raises(AIError) as caught:
                await Solver(store, ai, SimpleNamespace()).run(task)
            assert caught.value is error and caught.value.code == "timeout"
            assert ai.batches == [[1, 2, 3, 4], [5]]
            assert events == [[1, 2, 3, 4], [5], "closed"]
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
            events, _ = mock_pdf_batches(monkeypatch, list(range(1, 6)), total=13, selected=True)
            ai = Recognizer(transcript_size=13000)
            task = make_task(store, text="第1-5页")
            with pytest.raises(InputError, match="尚未完成全文处理.*页码"):
                await Solver(store, ai, SimpleNamespace()).run(task)
            assert len(ai.batches) == 2
            assert events == [[1, 2, 3, 4], [5], "closed"]
            current = store.get(task["task_id"])
            assert current["state"] == "reading" and not current["result"] and not current["problem"]
        finally:
            store.close()
    asyncio.run(check())


def test_real_thirteen_page_pdf_reads_only_explicit_selection(tmp_path, monkeypatch):
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
            task = make_task(store, text="第1-5页")
            ai = Recognizer()
            problem, _, notes = await Solver(store, ai, SimpleNamespace()).inputs(
                task, store.task_dir(task["task_id"]))
            assert list(map(len, ai.batches)) == [4, 1]
            assert "共 13 页，第 5 页" in problem
            assert "已读取指定页面" in notes and "共识别 5" in notes
            assert not list(store.task_dir(task["task_id"]).rglob("input.pdf"))
            assert not list(store.task_dir(task["task_id"]).rglob("preview-*.png"))
        finally:
            store.close()
    asyncio.run(check())
