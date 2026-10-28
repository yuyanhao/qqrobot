import asyncio
from collections import OrderedDict
import io
import re
from types import SimpleNamespace
from urllib.parse import urlparse

from PIL import Image
import pytest

from file_tools import DownloadError, DownloadedFile
from solver import InputError, Solver
from task_bot import RAW_EVENTS, TaskBot
from task_store import TaskStore


def png(number):
    buffer = io.BytesIO()
    Image.new("RGB", (3, 3), (number, 0, 0)).save(buffer, format="PNG")
    return buffer.getvalue()


def image_attachment(number):
    return {"url": f"https://example.com/page-{number}.png", "content_type": "image/png", "name": f"page-{number}.png"}


class Recognizer:
    def __init__(self):
        self.batches = []

    async def transcribe(self, images, user_text, **kwargs):
        numbers = []
        for data, mime in images:
            assert mime == "image/png"
            with Image.open(io.BytesIO(data)) as image:
                numbers.append(image.getpixel((0, 0))[0])
        self.batches.append(numbers)
        return {"problem_latex": "，".join(f"图片-{number}" for number in numbers), "notes": "模拟识图备注"}


def parent_task(store, owner="u"):
    task, _ = store.create(owner, "original", "c2c", "u", {
        "text": "原题文字", "attachments": [image_attachment(99)]})
    store.update(task["task_id"], state="completed", problem="原题：求参数 a 下的积分。",
                 result={"solution": {"answer_latex": "上次解答"}})
    return store.get(task["task_id"])


def mock_image_download(monkeypatch):
    calls = []

    async def download(url, **kwargs):
        number = int(re.search(r"page-(\d+)", urlparse(url).path).group(1))
        calls.append(number)
        return DownloadedFile(png(number), f"page-{number}.png", "image/png", url)

    monkeypatch.setattr("solver.download_file", download)
    return calls


@pytest.mark.parametrize("action", ["补充", "追问"])
@pytest.mark.parametrize("quoted", [False, True])
def test_image_only_followup_routes_current_or_quoted_attachments(tmp_path, action, quoted):
    async def check():
        store = TaskStore(tmp_path)
        bot = object.__new__(TaskBot)
        bot.store, bot.seen = store, OrderedDict()
        bot._ensure_worker = lambda: None
        submitted, replies = [], []

        async def submit(context, request):
            submitted.append(request)

        async def reply(context, text):
            replies.append(text)
            return True

        bot._submit, bot._text = submit, reply
        message_id = f"{action}-{quoted}"
        attachment = image_attachment(1)
        try:
            parent = parent_task(store)
            message = SimpleNamespace(id=message_id, content=f"/{action} {parent['task_id']}",
                                      attachments=[] if quoted else [attachment])
            if quoted:
                RAW_EVENTS[message_id] = {"message_type": 103, "msg_elements": [
                    {"content": "引用中的补充说明", "attachments": [attachment]}]}
            await bot._handle(message, "c2c", "u", "u")
            assert len(submitted) == 1, replies
            assert submitted[0]["parent_id"] == parent["task_id"]
            assert submitted[0]["action"] == action
            assert submitted[0]["attachments"] == [attachment]
            if quoted:
                assert "引用中的补充说明" in submitted[0]["quoted"]
        finally:
            RAW_EVENTS.pop(message_id, None)
            store.close()
    asyncio.run(check())


def test_missing_referenced_image_requests_resend_instead_of_empty_job(tmp_path):
    async def check():
        store = TaskStore(tmp_path)
        bot = object.__new__(TaskBot)
        bot.store, bot.seen = store, OrderedDict()
        bot._ensure_worker = lambda: None
        submitted, replies = [], []

        async def submit(context, request):
            submitted.append(request)

        async def reply(context, text):
            replies.append(text)
            return True

        bot._submit, bot._text = submit, reply
        try:
            parent = parent_task(store)
            RAW_EVENTS["empty-quote"] = {"message_type": 103, "msg_elements": []}
            await bot._handle(SimpleNamespace(id="empty-quote", content=f"/补充 {parent['task_id']}", attachments=[]),
                              "c2c", "u", "u")
            assert not submitted
            assert any("图片" in text and any(word in text for word in ("重发", "重新发送", "附上")) for text in replies)
        finally:
            RAW_EVENTS.pop("empty-quote", None)
            store.close()
    asyncio.run(check())


@pytest.mark.parametrize("action", ["补充", "追问"])
def test_followup_images_are_recognized_with_original_problem(tmp_path, monkeypatch, action):
    async def check():
        store = TaskStore(tmp_path)
        try:
            parent = parent_task(store)
            calls = mock_image_download(monkeypatch)
            ai = Recognizer()
            task, _ = store.create("u", "image-followup", "c2c", "u", {
                "parent_id": parent["task_id"], "action": action, "text": "", "quoted": "图片中的条件替换旧条件",
                "attachments": [image_attachment(1), image_attachment(2)]})
            problem, extra, notes = await Solver(store, ai, SimpleNamespace(input_pages=4)).inputs(
                task, store.task_dir(task["task_id"]))
            assert parent["problem"] in problem
            assert "图片-1" in problem and "图片-2" in problem
            assert "图片中的条件替换旧条件" in problem or "图片中的条件替换旧条件" in extra
            assert "上次解答" in extra
            assert calls == [1, 2]
            assert ai.batches == [[1, 2]]
        finally:
            store.close()
    asyncio.run(check())


def test_followup_pdf_pages_reach_recognition_and_keep_page_notes(tmp_path, monkeypatch):
    async def check():
        store = TaskStore(tmp_path)
        try:
            parent = parent_task(store)
            downloads, renders = [], []

            async def download(url, **kwargs):
                downloads.append(url)
                return DownloadedFile(b"synthetic PDF", "conditions.pdf", "application/pdf", url)

            async def render(data, directory, text, max_pages=0):
                renders.append((data, text, max_pages))
                yield [png(3), png(4)], 6, [3, 4], True

            monkeypatch.setattr("solver.download_file", download)
            monkeypatch.setattr("solver.render_input_pdf_batches_async", render)
            ai = Recognizer()
            task, _ = store.create("u", "pdf-followup", "c2c", "u", {
                "parent_id": parent["task_id"], "action": "补充", "text": "读取第3-4页",
                "attachments": [{"url": "https://example.com/conditions.pdf", "name": "conditions.pdf"}]})
            problem, extra, notes = await Solver(store, ai, SimpleNamespace(input_pages=4)).inputs(
                task, store.task_dir(task["task_id"]))
            assert parent["problem"] in problem and "图片-3" in problem and "图片-4" in problem
            assert ai.batches == [[3, 4]]
            assert len(downloads) == len(renders) == 1
            assert renders[0][1] == "读取第3-4页"
            assert renders[0][2] == 0
            assert "6" in notes and "3" in notes and "4" in notes
        finally:
            store.close()
    asyncio.run(check())


def test_followup_text_attachment_is_added_without_downloading_parent_again(tmp_path, monkeypatch):
    async def check():
        store = TaskStore(tmp_path)
        try:
            parent = parent_task(store)
            calls = []

            async def download(url, **kwargs):
                calls.append(url)
                return DownloadedFile("新增条件：a > 0。".encode("utf-8"), "condition.txt", "text/plain", url)

            monkeypatch.setattr("solver.download_file", download)
            task, _ = store.create("u", "text-followup", "c2c", "u", {
                "parent_id": parent["task_id"], "action": "补充", "text": "条件见附件",
                "attachments": [{"url": "https://example.com/condition.txt", "name": "condition.txt"}]})
            problem, _, _ = await Solver(store, None, SimpleNamespace(input_pages=4)).inputs(
                task, store.task_dir(task["task_id"]))
            assert parent["problem"] in problem and "新增条件：a > 0。" in problem
            assert calls == ["https://example.com/condition.txt"]
        finally:
            store.close()
    asyncio.run(check())


def test_supplemented_conditions_survive_another_followup(tmp_path):
    async def check():
        store = TaskStore(tmp_path)
        try:
            parent = parent_task(store)
            child, _ = store.create("u", "supplement", "c2c", "u", {
                "parent_id": parent["task_id"], "action": "补充", "text": "参数 a 必须为正数", "attachments": []})
            solver = Solver(store, None, SimpleNamespace(input_pages=4))
            problem, _, _ = await solver.inputs(child, store.task_dir(child["task_id"]))
            assert "参数 a 必须为正数" in problem
            store.update(child["task_id"], state="completed", problem=problem,
                         result={"solution": {"answer_latex": "使用补充条件后的解答"}})
            grandchild, _ = store.create("u", "followup", "c2c", "u", {
                "parent_id": child["task_id"], "action": "追问", "text": "解释收敛条件", "attachments": []})
            inherited, extra, _ = await solver.inputs(grandchild, store.task_dir(grandchild["task_id"]))
            assert "参数 a 必须为正数" in inherited and "解释收敛条件" in extra
        finally:
            store.close()
    asyncio.run(check())


@pytest.mark.parametrize("count", [5, 9, 12])
def test_collected_images_are_transcribed_in_order_in_batches_of_four(tmp_path, monkeypatch, count):
    async def check():
        store = TaskStore(tmp_path)
        try:
            calls = mock_image_download(monkeypatch)
            ai = Recognizer()
            task, _ = store.create("u", "collected", "c2c", "u", {
                "text": "所有图片属于同一道题", "collected": True,
                "attachments": [image_attachment(number) for number in range(1, count + 1)]})
            problem, _, _ = await Solver(store, ai, SimpleNamespace(input_pages=4)).inputs(
                task, store.task_dir(task["task_id"]))
            expected = list(range(1, count + 1))
            assert calls == expected
            assert ai.batches == [expected[index:index + 4] for index in range(0, count, 4)]
            assert list(map(int, re.findall(r"图片-(\d+)", problem))) == expected
        finally:
            store.close()
    asyncio.run(check())


@pytest.mark.parametrize("collected,count", [(False, 5), (True, 13)])
def test_attachment_limit_is_checked_before_downloading(tmp_path, monkeypatch, collected, count):
    async def check():
        store = TaskStore(tmp_path)
        try:
            calls = mock_image_download(monkeypatch)
            ai = Recognizer()
            task, _ = store.create("u", "too-many", "c2c", "u", {
                "text": "求解", "collected": collected,
                "attachments": [image_attachment(number) for number in range(1, count + 1)]})
            with pytest.raises(InputError):
                await Solver(store, ai, SimpleNamespace(input_pages=4)).inputs(task, store.task_dir(task["task_id"]))
            assert not calls and not ai.batches
        finally:
            store.close()
    asyncio.run(check())


def test_parent_image_download_failure_is_not_silently_ignored(tmp_path, monkeypatch):
    async def check():
        store = TaskStore(tmp_path)
        try:
            parent = parent_task(store)

            async def unavailable(*args, **kwargs):
                raise DownloadError("expired synthetic image URL")

            monkeypatch.setattr("solver.download_file", unavailable)
            task, _ = store.create("u", "bad-image", "c2c", "u", {
                "parent_id": parent["task_id"], "action": "补充", "text": "", "attachments": [image_attachment(1)]})
            with pytest.raises(InputError, match="重新上传|重发|下载失败"):
                await Solver(store, Recognizer(), SimpleNamespace(input_pages=4)).inputs(task, store.task_dir(task["task_id"]))
        finally:
            store.close()
    asyncio.run(check())
