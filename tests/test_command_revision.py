import asyncio
from contextlib import closing
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace

import pypdfium2 as pdfium
import pytest

from test_delivery_modes import ReplyContext, context, make_bot
from task_bot import RAW_EVENTS


def test_book_reference_is_global_and_uses_only_specified_pages(tmp_path):
    from book_library import BookLibrary

    library = BookLibrary(tmp_path)
    source = tmp_path / "book.pdf"
    with pdfium.PdfDocument.new() as pdf:
        pdf.new_page(100, 100).close()
        pdf.new_page(100, 100).close()
        pdf.save(source)
    with library.staged_pdf() as staged:
        shutil.copyfile(source, staged)
        book = library.add(staged, "数学分析", "级数与积分")
    first, remainder = library.reference(f"{book['id']} 第2页 解释例1")
    assert remainder == "解释例1"
    assert first["pdf_selection"] == "第2页"
    assert BookLibrary(tmp_path).path(first["book_id"]).is_file()


def test_book_can_read_selected_page_of_pdf_above_regular_render_limit(tmp_path):
    from pdf_tools import PDFError, render_pdf_batch

    source = tmp_path / "large.pdf"
    make_large_pdf(source, 11)
    with pytest.raises(PDFError, match="10 MB"):
        render_pdf_batch(source, "第2页")
    images, total, pages, selected, count = render_pdf_batch(source, "第2页", book=True)
    assert (total, pages, selected, count) == (2, [2], True, 1)
    assert len(images) == 1


def make_large_pdf(source, padding_mb):
    with pdfium.PdfDocument.new() as pdf:
        pdf.new_page(100, 100).close()
        pdf.new_page(100, 100).close()
        pdf.save(source)
    data = source.read_bytes()
    trailer = re.search(rb"(startxref\s+)(\d+)", data)
    start = int(trailer[2])
    with source.open("wb") as file:
        file.write(data[:start] + b"\n%")
        file.seek(padding_mb * 1024**2, 1)
        file.write(b"\n")
        updated_start = file.tell()
        file.write(data[start:trailer.start(2)])
        file.write(str(updated_start).encode())
        file.write(data[trailer.end(2):])


def test_collection_stores_each_pdf_selection_separately(tmp_path, monkeypatch):
    from test_collection_commands import make_bot as make_collection_bot

    async def download(url, *, max_mb, timeout):
        assert max_mb == 10
        return SimpleNamespace(data=b"same PDF")

    monkeypatch.setattr("input_cache.download_file", download)
    bot = make_collection_bot(tmp_path)

    async def scenario():
        ctx = context()
        attachment = {"url": "https://example.com/book.pdf", "name": "book.pdf"}
        await bot._create_collection(ctx, "只读第3-5页", "", [attachment])
        tid = bot.store.collecting(ctx.owner)["task_id"]
        for page in ("第3页", "只读第5页，解答例2"):
            ctx.message_id = page
            await bot._dispatch_command(ctx, f"/补图 {tid} {page}", "", [attachment])
        request = bot.store.get(tid)["request"]
        assert [item.get("pdf_selection") for item in request["attachments"]] == ["第3-5页", "第3页", "第5页"]
        assert request["text"] == "解答例2"

    try:
        asyncio.run(scenario())
    finally:
        bot.store.close()


@pytest.mark.parametrize("kind, text, current, quoted_pdf, starts_task", [
    ("c2c", "", True, False, False),
    ("c2c", "只读第2页", True, False, False),
    ("c2c", "", False, True, False),
    ("c2c", "只读第2页", False, True, True),
    ("c2c", "只读第2页", True, True, True),
    ("group", "只读第2页", True, False, True),
])
def test_private_pdf_starts_only_after_user_quotes_it_with_instructions(
        tmp_path, monkeypatch, kind, text, current, quoted_pdf, starts_task):
    bot = make_bot(tmp_path)
    pdf = {"url": "https://example.com/input.pdf", "content_type": "application/pdf", "name": "input.pdf"}
    message = SimpleNamespace(id="pdf-message", content=text, attachments=[pdf] if current else [])
    if quoted_pdf:
        monkeypatch.setitem(RAW_EVENTS, message.id, {"message_type": 103, "msg_elements": [
            {"content": "原消息中的说明", "attachments": [pdf]},
        ]})
    try:
        asyncio.run(bot._handle(message, kind, "u", "u"))
        if starts_task:
            task = bot.store.get(bot.queue.get_nowait())
            assert task["request"]["attachments"] == [pdf]
            assert task["request"]["text"] == text
        else:
            assert bot.queue.empty() and not bot.media.sent
            assert not bot.store.list_recent("u")
    finally:
        bot.store.close()


def test_group_pdf_without_page_selection_is_rejected_before_registration(tmp_path):
    bot = make_bot(tmp_path)
    pdf = {"url": "https://example.com/input.pdf", "content_type": "application/pdf", "name": "input.pdf"}
    message = SimpleNamespace(id="group-pdf", content="", attachments=[pdf])
    try:
        asyncio.run(bot._handle(message, "group", "g", "group:g:reader"))
        assert bot.queue.empty() and not bot.store.list_recent("group:g:reader")
        assert any("页码" in item[1] for item in bot.media.sent if item[0] == "text")
    finally:
        bot.store.close()


def test_pdf_page_budget_is_checked_before_registration(tmp_path):
    bot = make_bot(tmp_path)
    pdfs = [{"url": f"https://example.com/{index}.pdf", "name": f"{index}.pdf"}
            for index in range(5)]
    try:
        asyncio.run(bot._submit(context(), {"text": "第1-5页", "attachments": pdfs}))
        assert bot.queue.empty() and not bot.store.list_recent("u")
        assert any("超过上限" in item[1] for item in bot.media.sent if item[0] == "text")

        asyncio.run(bot._submit(context(), {
            "text": "解释一下", "parent_id": "Q261116-000001",
            "attachments": [pdfs[0]]}))
        assert bot.queue.empty()
        assert any("明确指定页码" in item[1] for item in bot.media.sent if item[0] == "text")

        asyncio.run(bot._submit(context(), {
            "text": "第2-3页 解释一下", "parent_id": "Q261116-000001",
            "attachments": [pdfs[0]]}))
        assert bot.queue.qsize() == 1
        task = bot.store.get(bot.queue.get_nowait())
        assert task["request"]["attachments"] == [pdfs[0]]
    finally:
        bot.store.close()


def test_secondary_pdf_selection_is_required_and_total_budget_is_five():
    from task_queue import TaskQueue
    from pdf_tools import PDFError

    book = {"book_id": "B0001", "name": "book.pdf", "pdf_selection": "第1-3页"}
    draft = {"url": "https://example.com/draft.pdf", "name": "draft.pdf"}
    with pytest.raises(PDFError, match="明确指定页码"):
        TaskQueue._pdf_page_budget({"parent_id": "Q261116-000001", "attachments": [book, draft]})

    selected_draft = {**draft, "pdf_selection": "第4-6页"}
    with pytest.raises(PDFError, match="超过上限 5 页"):
        TaskQueue._pdf_page_budget({"parent_id": "Q261116-000001",
                                    "attachments": [book, selected_draft]})

    one_book_page = {**book, "pdf_selection": "第1页"}
    two_draft_pages = {**draft, "pdf_selection": "第4-5页"}
    request = {"parent_id": "Q261116-000001", "attachments": [one_book_page, two_draft_pages]}
    assert TaskQueue._pdf_page_budget(request) == 3
    assert TaskQueue._estimated_ocr_batches(request) == 2


def test_ambiguous_book_attachment_detection(tmp_path):
    bot = make_bot(tmp_path)
    assert bot._is_ambiguous_book_attachment(
        {"name": "book", "content_type": "", "url": "https://example.com/download"})
    assert not bot._is_ambiguous_book_attachment(
        {"name": "book.pdf", "content_type": "", "url": "https://example.com/download"})
    assert not bot._is_ambiguous_book_attachment(
        {"name": "photo", "content_type": "image/png", "url": "https://example.com/download"})
    bot.store.close()


def test_admin_image_is_not_swallowed_by_book_staging(tmp_path):
    bot = make_bot(tmp_path)
    bot.book_admin_openid = "admin"
    image = {"url": "https://example.com/question.png", "name": "question.png", "content_type": "image/png"}
    message = SimpleNamespace(id="admin-image", content="", attachments=[image])
    try:
        asyncio.run(bot._handle(message, "c2c", "admin", "c2c:admin"))
        assert bot.queue.qsize() == 1
        task = bot.store.get(bot.queue.get_nowait())
        assert task["request"]["attachments"] == [image]
    finally:
        bot.store.close()


def test_check_draft_pdf_requires_an_explicit_page_selection(tmp_path):
    bot = make_bot(tmp_path)
    question = {"url": "https://example.com/question.png", "name": "question.png"}
    draft = {"url": "https://example.com/draft.pdf", "name": "draft.pdf"}
    try:
        asyncio.run(bot._dispatch_command(context(), "/查错 我令 t=x", "", [question, draft]))
        assert bot.queue.empty()
        assert any("明确指定页码" in item[1] for item in bot.media.sent if item[0] == "text")
    finally:
        bot.store.close()


def test_check_rejects_quoted_question_pdf_without_page_selection(tmp_path):
    bot = make_bot(tmp_path)
    question = {"url": "https://example.com/question.pdf", "name": "question.pdf"}
    try:
        asyncio.run(bot._dispatch_command(
            context(), "/查错 我令 t=x", "引用题面", [question],
            quoted_urls={question["url"]}))
        assert bot.queue.empty()
        assert any("明确指定页码" in item[1] for item in bot.media.sent if item[0] == "text")
    finally:
        bot.store.close()


def test_check_two_images_treats_first_as_question_and_second_as_draft(tmp_path):
    bot = make_bot(tmp_path)
    attachments = [{"url": f"https://example.com/{name}.png", "name": name} for name in ("question", "draft")]
    try:
        asyncio.run(bot._dispatch_command(context(), "/查错", "", attachments))
        task = bot.store.get(bot.queue.get_nowait())
        assert task["request"]["action"] == "查错"
        assert task["request"]["question_attachments"] == attachments[:1]
        assert task["request"]["attachments"] == attachments[1:]
    finally:
        bot.store.close()


@pytest.mark.parametrize("selection,quoted", [("第3页 ", False), ("只读第3页，", True)])
def test_check_keeps_pdf_page_selection_separate_from_draft(tmp_path, selection, quoted):
    bot = make_bot(tmp_path)
    attachment = {"url": "https://example.com/problem.pdf", "name": "problem.pdf"}
    draft = "我令 t=x，并令 Ａ=1。"
    try:
        urls = {attachment["url"]} if quoted else ()
        asyncio.run(bot._dispatch_command(context(), "/查错 " + selection + draft,
                                          "引用题面" if quoted else "", [attachment], quoted_urls=urls))
        assert not bot.queue.empty()
        request = bot.store.get(bot.queue.get_nowait())["request"]
        assert request["question_attachments"] == [{**attachment, "pdf_selection": "第3页"}]
        assert request["text"] == draft and not request["attachments"]
    finally:
        bot.store.close()


def test_check_applies_pages_to_quoted_question_pdf_and_attached_draft_pdf(tmp_path):
    bot = make_bot(tmp_path)
    question = {"url": "https://example.com/question.pdf", "name": "question.pdf"}
    draft = {"url": "https://example.com/draft.pdf", "name": "draft.pdf"}
    try:
        asyncio.run(bot._dispatch_command(
            context(), "/查错 第3页 我令 t=x", "引用题面", [question, draft],
            quoted_urls={question["url"]}))
        task = bot.store.get(bot.queue.get_nowait())
        assert task["request"]["question_attachments"] == [{**question, "pdf_selection": "第3页"}]
        assert task["request"]["attachments"] == [{**draft, "pdf_selection": "第3页"}]
        assert task["request"]["text"] == "我令 t=x"
    finally:
        bot.store.close()


@pytest.mark.parametrize("quoted_image", [True, False])
def test_check_treats_the_quoted_problem_and_current_draft_as_separate_materials(
        tmp_path, monkeypatch, quoted_image):
    bot = make_bot(tmp_path)
    problem = {"url": "https://example.com/problem.png"}
    draft = {"url": "https://example.com/draft.png"}
    message = SimpleNamespace(id="with-quote", content="/查错", attachments=[draft])
    monkeypatch.setitem(RAW_EVENTS, message.id, {"message_type": 103, "msg_elements": [
        {"content": "求函数的极限", "attachments": [problem] if quoted_image else []},
    ]})
    try:
        asyncio.run(bot._handle(message, "group", "g", "group:g:reader"))
        assert not bot.queue.empty()
        request = bot.store.get(bot.queue.get_nowait())["request"]
        assert [item["url"] for item in request["attachments"]] == [draft["url"]]
        assert [item["url"] for item in request["question_attachments"]] == (
            [problem["url"]] if quoted_image else [])
        assert request["problem_snapshot"] == "求函数的极限"
        assert not request["quoted"]
    finally:
        bot.store.close()


def test_removed_commands_are_excluded_from_help_and_panel():
    from qq_panel import GROUP_COMMANDS
    from task_commands import HELP

    for command in ("提示", "详解", "文字", "重排", "原解", "会了", "再练"):
        assert f"/{command}" not in HELP
        assert f"/{command}" not in dict(GROUP_COMMANDS)


def test_age_cleanup_deletes_rows_and_files_keeps_favorites(tmp_path):
    bot = make_bot(tmp_path)
    try:
        tasks = [bot.store.create("u", message, "c2c", "u", {})[0]["task_id"] for message in ("drop", "keep")]
        for tid in tasks:
            (bot.store.task_dir(tid) / "notes.txt").write_text("answer")
            bot.store.update(tid, state="completed")
        bot.store.save_favorite(tasks[1], "u", "stem", "answer")
        with bot.store.db:
            bot.store.db.execute("UPDATE tasks SET created=?", (time.time() - 8 * 86400,))
        bot.store.cleanup()
        assert bot.store.get(tasks[0]) is None and not (bot.store.results / tasks[0]).exists()
        assert bot.store.get(tasks[1]) and (bot.store.results / tasks[1] / "notes.txt").is_file()
    finally:
        bot.store.close()


def test_scheduled_cleanup_runs_without_incoming_messages(tmp_path, monkeypatch):
    bot = make_bot(tmp_path)
    bot.collection_lock = asyncio.Lock()
    task, _ = bot.store.create("u", "old", "c2c", "u", {})
    tid = task["task_id"]
    bot.store.update(tid, state="completed")
    with bot.store.db:
        bot.store.db.execute("UPDATE tasks SET created=?", (time.time() - 8 * 86400,))
    intervals = []

    async def tick(seconds):
        intervals.append(seconds)
        if len(intervals) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr("task_bot.asyncio.sleep", tick)
    try:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(bot._cleanup_periodically())
        assert intervals == [3600, 3600]
        assert bot.store.get(tid) is None
    finally:
        bot.store.close()


def test_managing_book_from_cloud_cli_uses_same_catalog(tmp_path):
    from book_library import BookLibrary
    from pdf_tools import render_pdf_batch

    source = tmp_path / "book.pdf"
    make_large_pdf(source, 11)
    script = Path(__file__).resolve().parents[1] / "book_library.py"
    cli = [sys.executable, str(script), "--data-dir", str(tmp_path)]

    def run(*args):
        return subprocess.run([*cli, *args], capture_output=True, encoding="utf-8", check=True).stdout

    assert "B0001" in run("add", str(source), "Real Analysis", "--summary", "Limits")
    assert "Real Analysis" in run("list")
    stored = BookLibrary(tmp_path).path("B0001")
    assert stored.stat().st_size == source.stat().st_size > 10 * 1024**2
    images, total, pages, selected, count = render_pdf_batch(stored, "第2页", book=True)
    assert (len(images), total, pages, selected, count) == (1, 2, [2], True, 1)
    assert "B0001" in run("remove", "B0001")
    assert not run("list")


def test_book_rejects_pdf_above_the_configured_size_limit(tmp_path, monkeypatch):
    import book_library
    from book_library import BookLibrary
    from pdf_tools import PDFError

    source = tmp_path / "oversized.pdf"
    make_large_pdf(source, 2)
    monkeypatch.setattr(book_library, "MAX_BOOK_PDF_MB", 1)
    library = BookLibrary(tmp_path)
    with library.staged_pdf() as staged:
        shutil.copyfile(source, staged)
        with pytest.raises(PDFError, match="不能超过"):
            library.add(staged, "Oversized")
    assert not library.list(-1)


def test_book_rejects_library_above_the_configured_quota(tmp_path, monkeypatch):
    import book_library
    from book_library import BookLibrary
    from pdf_tools import PDFError

    source = tmp_path / "book.pdf"
    make_large_pdf(source, 2)
    monkeypatch.setattr(book_library, "MAX_BOOK_LIBRARY_MB", 3)
    library = BookLibrary(tmp_path)
    with library.staged_pdf() as staged:
        shutil.copyfile(source, staged)
        library.add(staged, "First")
    with library.staged_pdf() as staged:
        shutil.copyfile(source, staged)
        with pytest.raises(PDFError, match="总容量"):
            library.add(staged, "Second")
    assert [book["title"] for book in library.list(-1)] == ["First"]


def test_invalid_model_tex_still_creates_readable_pdf(tmp_path):
    if not shutil.which("xelatex"):
        pytest.skip("XeLaTeX unavailable")
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"mode": "solution", "doc": {
        "title": "Malformed formula",
        "problem_latex": "Retry content remains visible",
        "steps": [{"text": "Derive the estimate", "math": r"x^{1+2"}],
        "answer_latex": "Result 42",
    }}), encoding="utf-8")
    worker = Path(__file__).resolve().parents[1] / "render_worker.py"
    result = subprocess.run([sys.executable, str(worker), request.name], cwd=tmp_path,
                            capture_output=True, encoding="utf-8", timeout=40)
    assert result.returncode == 0, result.stderr
    metadata = json.loads(result.stdout)
    assert metadata["plot_warnings"]
    with pdfium.PdfDocument(metadata["pdf_path"]) as document:
        with closing(document[0]) as page, closing(page.get_textpage()) as text:
            assert "Result 42" in text.get_text_range()


def test_admin_private_book_import_can_be_referenced_in_any_group(tmp_path, monkeypatch):
    from book_library import BookLibrary
    from solver import Solver

    source = tmp_path / "book.pdf"
    with pdfium.PdfDocument.new() as pdf:
        pdf.new_page(100, 100).close()
        pdf.save(source)
    incoming = [{"url": "https://example.com/book.pdf", "name": "book.pdf"}]
    bot = make_bot(tmp_path)
    bot.book_import_lock = asyncio.Lock()
    bot.collection_lock = asyncio.Lock()
    bot.book_admin_openid = "admin"
    bot.settings.sympy = False
    bot.settings.solve_timeout = 60
    reads = []

    async def download(url, *, max_mb, timeout, output=None):
        reads.append(url)
        assert 1 <= max_mb <= 100 and timeout == 30 * 60
        with source.open("rb") as file:
            shutil.copyfileobj(file, output)

    monkeypatch.setattr("task_commands.download_file", download)

    async def transcribe(images, text, **options):
        assert len(images) == 1
        return {"problem_latex": "合成 PDF 题干"}

    async def study(action, problem, material, **options):
        assert "合成 PDF 题干" in problem and material == "我令 t=x"
        return {"status": "complete", "feedback": "检查完成", "checks": []}

    async def scenario():
        group = ReplyContext("group", "g", "group:g:admin", "m1", time.time())
        other = ReplyContext("c2c", "other", "c2c:other", "m2", time.time())
        admin = ReplyContext("c2c", "admin", "c2c:admin", "m3", time.time())
        for visitor in (group, other):
            await bot._dispatch_command(visitor, "/入库 数学分析", "", incoming)
            assert "管理员" in bot.media.sent[-1][1]
        message = SimpleNamespace(id=admin.message_id, content="/入库 数学分析 | 极限", attachments=incoming)
        await bot._handle(message, admin.kind, admin.target, admin.owner)
        assert reads == [incoming[0]["url"]]
        jobs = []
        for gid, command, field in (
                ("g", "/引用 B0001 第1页 推导例题", "attachments"),
                ("h", "/查错 B0001 第1页 我令 t=x", "question_attachments")):
            chat = ReplyContext("group", gid, f"group:{gid}:reader", "read", time.time())
            await bot._dispatch_command(chat, "/引用", "", [])
            assert "B0001" in bot.media.sent[-1][1] and "极限" in bot.media.sent[-1][1]
            await bot._dispatch_command(chat, command, "", [])
            job = bot.store.get(bot.queue.get_nowait())
            assert job["owner"] == chat.owner
            assert job["request"][field][0]["book_id"] == "B0001"
            jobs.append(job)
        await bot._dispatch_command(chat, "/引用 B0001 解答一下", "", [])
        assert bot.queue.empty()
        assert "请填写书籍编号和物理页码" in bot.media.sent[-1][1]
        library = BookLibrary(tmp_path)
        saved_pdf = library.path("B0001")
        await bot._dispatch_command(other, "/移除书籍 B0001", "", [])
        assert "管理员" in bot.media.sent[-1][1]
        await bot._dispatch_command(admin, "/移除书籍 B0001", "", [])
        assert "已移除 B0001" in bot.media.sent[-1][1]
        assert not library.list(-1)
        assert saved_pdf.is_file()
        await bot._dispatch_command(chat, "/引用 B0001 第1页", "", [])
        assert "B0001" in bot.media.sent[-1][1] and bot.queue.empty()

        pipeline = Solver(bot.store, SimpleNamespace(transcribe=transcribe, study=study), bot.settings)
        direct, checking = jobs
        problem, _, _ = await pipeline.inputs(direct, bot.store.task_dir(direct["task_id"]))
        assert "合成 PDF 题干" in problem
        bot.store.update(direct["task_id"], state="completed")
        await pipeline.run(checking)
        assert bot.store.get(checking["task_id"])["result"]["study_text"] == "检查完成"

        intervals = []

        async def sleep(delay):
            intervals.append(delay)
            if len(intervals) > 1:
                raise asyncio.CancelledError

        monkeypatch.setattr("task_bot.asyncio.sleep", sleep)
        with pytest.raises(asyncio.CancelledError):
            await bot._cleanup_periodically()
        assert not saved_pdf.exists()

    try:
        asyncio.run(scenario())
    finally:
        bot.store.close()


@pytest.mark.skipif(not shutil.which("xelatex"), reason="XeLaTeX unavailable")
def test_favorite_selector_and_pdf_use_saved_order_and_owner(tmp_path):
    bot = make_bot(tmp_path)
    captured = []

    async def upload(kind, target, path):
        with pdfium.PdfDocument(path) as document:
            pages = []
            for index in range(len(document)):
                with closing(document[index]) as page, closing(page.get_textpage()) as text:
                    pages.append(text.get_text_range())
        captured.append((path.name, "\n".join(pages)))
        return path.name

    bot.media.upload_file_path = upload
    first, second = "Q261115-000009", "Q261115-000001"
    bot.store.save_favorite(first, "u", "First question", "First answer", "First mistake")
    bot.store.save_favorite(second, "u", "Second question", "Second answer")
    bot.store.save_favorite("Q261115-000003", "other", "Other question", "Private answer")

    async def scenario():
        await bot._dispatch_command(context(), "/复习", "", [])
        chooser = bot.media.sent[-1][1]
        assert chooser.index(first) < chooser.index(second)
        assert "First answer" not in chooser
        await bot._dispatch_command(context(), f"/复习 {first}", "", [])
        await bot._dispatch_command(context(), "/错题本", "", [])

    try:
        asyncio.run(scenario())
        (review_name, review), (export_name, export) = captured
        assert review_name.endswith(first + ".pdf") and export_name == "错题本.pdf"
        assert all(part in review for part in ("First question", "First answer", "First mistake"))
        assert export.index("First answer") < export.index("Second answer")
        assert "Private answer" not in export
    finally:
        bot.store.close()


def test_cancelled_job_waiting_for_resource_lock_does_not_start(tmp_path):
    instance = make_bot(tmp_path)

    async def check():
        async def unexpected_solve(task):
            pytest.fail("A cancelled job must not reach the solver")

        instance.solver = SimpleNamespace(run=unexpected_solve)
        instance.running_id = instance.running_task = None
        task, _ = instance.store.create("u", "queued", "group", "g", {"text": "测试题"})
        tid = task["task_id"]
        instance.queue = asyncio.Queue()
        instance.contexts = {}
        instance.queue.put_nowait(tid)
        async with instance.resource_lock:
            worker = asyncio.create_task(instance._worker())
            await asyncio.sleep(0)
            await instance._dispatch_command(context(), f"/取消 {tid}", "", [])
            assert "已取消" in instance.media.sent[-1][1]
        try:
            await asyncio.wait_for(instance.queue.join(), timeout=1)
        finally:
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker
        assert instance.store.get(tid)["state"] == "cancelled"

    try:
        asyncio.run(check())
    finally:
        instance.store.close()
