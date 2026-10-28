import asyncio
from collections import OrderedDict
import io
from pathlib import Path
import re
from types import SimpleNamespace
from urllib.parse import urlparse

from PIL import Image
import pytest

from file_tools import DownloadedFile
from solver import Solver
from task_bot import HELP, RAW_EVENTS, TaskBot
from task_store import TaskStore


class FakeMedia:
    def __init__(self):
        self.sent = []

    async def send_text(self, kind, target, text, msg_id=None, msg_seq=None):
        self.sent.append(text)


def make_bot(tmp_path):
    bot = object.__new__(TaskBot)
    bot.store = TaskStore(tmp_path)
    bot.media = FakeMedia()
    bot.settings = SimpleNamespace(
        active_delivery=False, job_timeout=1200, result_ttl=7, disk_limit_mb=512,
        max_queue=5, max_user_tasks=2, max_daily=50, input_pages=4)
    bot.resource_lock, bot.collection_lock = asyncio.Lock(), asyncio.Lock()
    bot.queue = asyncio.Queue(maxsize=5)
    bot.contexts, bot.seen = {}, OrderedDict()
    bot.running_task = bot.running_id = None
    bot._ensure_worker = lambda: None
    return bot


def png(number):
    buffer = io.BytesIO()
    Image.new("RGB", (3, 3), (number, 0, 0)).save(buffer, format="PNG")
    return buffer.getvalue()


def attachment(number, token=""):
    return {"url": f"https://example.com/page-{number}.png{token}",
            "name": f"page-{number}.png", "content_type": "image/png"}


def mock_cache_download(monkeypatch):
    calls = []

    async def download(url, **kwargs):
        number = int(re.search(r"page-(\d+)", urlparse(url).path).group(1))
        calls.append(number)
        return DownloadedFile(png(number), f"page-{number}.png", "image/png", url)

    monkeypatch.setattr("input_cache.download_file", download)
    return calls


async def handle(bot, text, message_id, attachments=(), owner="u"):
    message = SimpleNamespace(id=message_id, content=text, attachments=list(attachments))
    await bot._handle(message, "c2c", "u", owner)


def test_collect_append_and_start_keep_one_task_and_wait_for_all_images(tmp_path, monkeypatch):
    async def check():
        bot = make_bot(tmp_path)
        try:
            calls = mock_cache_download(monkeypatch)
            await handle(bot, "/收集 所有图片属于同一道题", "create", [attachment(1)])
            draft = bot.store.collecting("u")
            assert draft is not None and draft["state"] == "collecting"
            tid = draft["task_id"]
            assert bot.queue.empty() and "所有图片属于同一道题" in draft["request"]["text"]
            assert len(draft["request"]["attachments"]) == 1
            await handle(bot, f"/补图 {tid}", "second", [attachment(2), attachment(3)])
            collected = bot.store.get(tid)
            assert len(bot.store.list_recent("u")) == 1
            assert collected["state"] == "collecting" and bot.queue.empty()
            assert len(collected["request"]["attachments"]) == 3
            assert calls == [1, 2, 3]
            assert f"/开始 {tid}" in "\n".join(bot.media.sent)
            await handle(bot, f"/开始 {tid}", "start")
            assert bot.store.get(tid)["state"] == "queued"
            assert bot.queue.qsize() == 1 and bot.queue.get_nowait() == tid
            await handle(bot, f"/开始 {tid}", "repeat-start")
            assert bot.queue.empty()
        finally:
            bot.store.close()
    asyncio.run(check())


def test_empty_collection_cannot_start_and_materials_can_be_added_later(tmp_path, monkeypatch):
    async def check():
        bot = make_bot(tmp_path)
        try:
            mock_cache_download(monkeypatch)
            await handle(bot, "/收集", "create")
            draft = bot.store.collecting("u")
            assert draft is not None
            await handle(bot, f"/开始 {draft['task_id']}", "empty-start")
            assert bot.queue.empty() and bot.store.get(draft["task_id"])["state"] == "collecting"
            await handle(bot, f"/补图 {draft['task_id']}", "photo", [attachment(1)])
            await handle(bot, f"/开始 {draft['task_id']}", "real-start")
            assert bot.queue.qsize() == 1
        finally:
            bot.store.close()
    asyncio.run(check())


def test_supplement_command_appends_text_and_image_to_collecting_task(tmp_path, monkeypatch):
    async def check():
        bot = make_bot(tmp_path)
        try:
            mock_cache_download(monkeypatch)
            await handle(bot, "/收集 原题文字", "create")
            tid = bot.store.collecting("u")["task_id"]
            await handle(bot, f"/补充 {tid} 设 x 大于零", "text")
            await handle(bot, f"/补充 {tid}", "image", [attachment(1)])
            task = bot.store.get(tid)
            assert task["state"] == "collecting" and bot.queue.empty()
            assert len(bot.store.list_recent("u")) == 1
            assert "设 x 大于零" in task["request"]["text"]
            assert len(task["request"]["attachments"]) == 1
        finally:
            bot.store.close()
    asyncio.run(check())


def test_collected_images_survive_restart_and_url_expiry(tmp_path, monkeypatch):
    async def check():
        from input_cache import read_cached_attachment

        bot = make_bot(tmp_path)
        try:
            mock_cache_download(monkeypatch)
            await handle(bot, "/收集", "create", [attachment(1)])
            tid = bot.store.collecting("u")["task_id"]
            bot.store.close()
            bot.store = TaskStore(tmp_path)
            bot.store.recover()
            restored = bot.store.get(tid)
            assert restored["state"] == "collecting"
            item = restored["request"]["attachments"][0]
            assert read_cached_attachment(bot.store, restored, item) == png(1)

            async def expired(*args, **kwargs):
                raise AssertionError("saved local images must not be downloaded again")

            monkeypatch.setattr("solver.download_file", expired)
            monkeypatch.setattr("input_cache.download_file", expired)
            await handle(bot, f"/开始 {tid}", "start")
            assert bot.queue.qsize() == 1

            class AI:
                async def transcribe(self, images, user_text):
                    assert len(images) == 1
                    return {"problem_latex": "缓存中的数学题", "notes": ""}

            problem, _, _ = await Solver(bot.store, AI(), bot.settings).inputs(
                bot.store.get(tid), bot.store.task_dir(tid))
            assert "缓存中的数学题" in problem
        finally:
            bot.store.close()
    asyncio.run(check())


def test_collecting_multiple_supplement_images_keeps_parent_problem(tmp_path, monkeypatch):
    async def check():
        bot = make_bot(tmp_path)
        try:
            mock_cache_download(monkeypatch)
            parent, _ = bot.store.create("u", "parent", "c2c", "u", {"text": "原题"})
            bot.store.update(parent["task_id"], state="completed", problem="原题：求积分。",
                             result={"solution": {"answer_latex": "旧解答"}})
            await handle(bot, f"/收集 {parent['task_id']} 以下图片修正原题条件", "create")
            draft = bot.store.collecting("u")
            assert draft is not None and draft["task_id"] != parent["task_id"]
            assert draft["request"]["parent_id"] == parent["task_id"]
            assert draft["request"]["action"] == "补充"
            tid = draft["task_id"]
            for number in (1, 2):
                await handle(bot, f"/补图 {tid}", f"image-{number}", [attachment(number)])
            assert bot.queue.empty()
            await handle(bot, f"/开始 {tid}", "start")

            class AI:
                async def transcribe(self, images, user_text):
                    assert len(images) == 2
                    return {"problem_latex": "图片中的修正条件", "notes": ""}

            problem, extra, _ = await Solver(bot.store, AI(), bot.settings).inputs(
                bot.store.get(tid), bot.store.task_dir(tid))
            assert "原题：求积分。" in problem and "图片中的修正条件" in problem
            assert "以下图片修正原题条件" in problem
            assert "旧解答" in extra
        finally:
            bot.store.close()
    asyncio.run(check())


def test_quoted_photo_can_be_added_to_collection(tmp_path, monkeypatch):
    async def check():
        bot = make_bot(tmp_path)
        try:
            calls = mock_cache_download(monkeypatch)
            await handle(bot, "/收集", "create")
            tid = bot.store.collecting("u")["task_id"]
            RAW_EVENTS["quoted-image"] = {"message_type": 103, "msg_elements": [
                {"content": "引用中的条件", "attachments": [attachment(1)]}]}
            await handle(bot, f"/补图 {tid}", "quoted-image")
            task = bot.store.get(tid)
            assert len(task["request"]["attachments"]) == 1
            assert "引用中的条件" in task["request"]["quoted"]
            assert calls == [1]
        finally:
            RAW_EVENTS.pop("quoted-image", None)
            bot.store.close()
    asyncio.run(check())


def test_missing_quoted_photo_does_not_create_empty_material(tmp_path, monkeypatch):
    async def check():
        bot = make_bot(tmp_path)
        try:
            calls = mock_cache_download(monkeypatch)
            await handle(bot, "/收集", "create")
            tid = bot.store.collecting("u")["task_id"]
            RAW_EVENTS["empty-quote"] = {"message_type": 103, "msg_elements": []}
            await handle(bot, f"/补图 {tid}", "empty-quote")
            assert not calls and not bot.store.get(tid)["request"]["attachments"]
            assert any("图片" in text and any(word in text for word in ("重发", "重新发送", "附上"))
                       for text in bot.media.sent)
        finally:
            RAW_EVENTS.pop("empty-quote", None)
            bot.store.close()
    asyncio.run(check())


def test_duplicate_image_content_is_not_added_twice(tmp_path, monkeypatch):
    async def check():
        bot = make_bot(tmp_path)
        try:
            mock_cache_download(monkeypatch)
            await handle(bot, "/收集", "create")
            tid = bot.store.collecting("u")["task_id"]
            await handle(bot, f"/补图 {tid}", "image-1", [attachment(1)])
            await handle(bot, f"/补图 {tid}", "image-1-other-url", [attachment(1, "?token=changed")])
            assert len(bot.store.get(tid)["request"]["attachments"]) == 1
            assert bot.queue.empty()
        finally:
            bot.store.close()
    asyncio.run(check())


@pytest.mark.parametrize("token", ["", "?token=changed"])
def test_duplicate_photo_is_acknowledged_as_saved_with_zero_additions(tmp_path, monkeypatch, token):
    async def check():
        bot = make_bot(tmp_path)
        try:
            calls = mock_cache_download(monkeypatch)
            await handle(bot, "/收集", "create", [attachment(1)])
            tid = bot.store.collecting("u")["task_id"]
            bot.media.sent.clear()
            await handle(bot, f"/补图 {tid}", "duplicate-photo", [attachment(1, token)])
            assert len(bot.store.get(tid)["request"]["attachments"]) == 1
            assert bot.queue.empty()
            assert "已保存" in bot.media.sent[-1] and "新增 0" in bot.media.sent[-1]
            assert "材料未追加" not in bot.media.sent[-1]
            assert calls == ([1] if not token else [1, 1])
        finally:
            bot.store.close()
    asyncio.run(check())


def test_start_during_initial_receipt_waits_for_first_photo(tmp_path, monkeypatch):
    async def check():
        bot = make_bot(tmp_path)
        starts = []
        original_send = bot.media.send_text

        async def start_on_receipt(kind, target, text, msg_id=None, msg_seq=None):
            await original_send(kind, target, text, msg_id, msg_seq)
            if msg_id == "create" and not starts:
                tid = bot.store.collecting("u")["task_id"]
                starts.append(asyncio.create_task(handle(bot, f"/开始 {tid}", "concurrent-start")))
                # Let the start command reach its first await while the initial
                # receipt is still pending. It must not outrun the first cache.
                await asyncio.sleep(0)

        bot.media.send_text = start_on_receipt
        try:
            mock_cache_download(monkeypatch)
            await handle(bot, "/收集 求图中函数的极限", "create", [attachment(1)])
            await asyncio.wait_for(asyncio.gather(*starts), timeout=3)
            assert bot.queue.qsize() == 1
            tid = bot.queue.get_nowait()
            queued = bot.store.get(tid)
            assert queued["state"] == "queued"
            assert len(queued["request"]["attachments"]) == 1
            assert not queued["error"]
            assert not any("已不在收集状态" in text for text in bot.media.sent)
        finally:
            for pending in starts:
                if not pending.done():
                    pending.cancel()
            if starts:
                await asyncio.gather(*starts, return_exceptions=True)
            bot.store.close()
    asyncio.run(check())


def test_initial_photo_failure_blocks_start_until_photo_is_saved_after_restart(tmp_path, monkeypatch):
    async def check():
        from file_tools import DownloadError

        bot = make_bot(tmp_path)

        async def failed_download(*args, **kwargs):
            raise DownloadError("QQ 图片地址暂时不可用")

        try:
            monkeypatch.setattr("input_cache.download_file", failed_download)
            await handle(bot, "/收集 求图中函数的极限", "create", [attachment(1)])
            tid = bot.store.collecting("u")["task_id"]
            failed = bot.store.get(tid)
            assert failed["state"] == "collecting" and not failed["request"]["attachments"]
            assert "首批附件未保存" in failed["error"]
            assert not list((bot.store.task_dir(tid) / "inputs").glob("*.bin"))

            bot.store.close()
            bot.store = TaskStore(tmp_path)
            bot.store.recover()
            await handle(bot, f"/开始 {tid}", "start-after-restart")
            assert bot.queue.empty() and bot.store.get(tid)["state"] == "collecting"
            assert "补图" in bot.media.sent[-1]

            # Adding unrelated text cannot silently waive the failed original photo.
            await handle(bot, f"/补充 {tid} 需要详细过程", "text-supplement")
            await handle(bot, f"/开始 {tid}", "start-after-text")
            assert bot.queue.empty() and bot.store.get(tid)["state"] == "collecting"
            assert "首批附件未保存" in bot.store.get(tid)["error"]

            mock_cache_download(monkeypatch)
            await handle(bot, f"/补图 {tid}", "replacement-photo", [attachment(1)])
            assert len(bot.store.get(tid)["request"]["attachments"]) == 1
            assert not bot.store.get(tid)["error"]
            await handle(bot, f"/开始 {tid}", "start-after-photo")
            assert bot.queue.qsize() == 1 and bot.store.get(tid)["state"] == "queued"
        finally:
            bot.store.close()
    asyncio.run(check())


def test_collection_limit_rejects_addition_without_losing_existing_photos(tmp_path, monkeypatch):
    async def check():
        bot = make_bot(tmp_path)
        try:
            mock_cache_download(monkeypatch)
            await handle(bot, "/收集", "create")
            tid = bot.store.collecting("u")["task_id"]
            for start in (1, 5, 9):
                await handle(bot, f"/补图 {tid}", f"batch-{start}",
                             [attachment(number) for number in range(start, start + 4)])
            assert len(bot.store.get(tid)["request"]["attachments"]) == 12
            await handle(bot, f"/补图 {tid}", "thirteenth", [attachment(13)])
            assert len(bot.store.get(tid)["request"]["attachments"]) == 12
            assert bot.store.get(tid)["state"] == "collecting" and bot.queue.empty()
            assert "12" in bot.media.sent[-1]
        finally:
            bot.store.close()
    asyncio.run(check())


def test_other_user_cannot_add_or_start_collection(tmp_path, monkeypatch):
    async def check():
        bot = make_bot(tmp_path)
        try:
            calls = mock_cache_download(monkeypatch)
            await handle(bot, "/收集", "create")
            tid = bot.store.collecting("u")["task_id"]
            await handle(bot, f"/补图 {tid}", "intruder-image", [attachment(1)], owner="another-user")
            await handle(bot, f"/开始 {tid}", "intruder-start", owner="another-user")
            assert not calls and not bot.store.get(tid)["request"]["attachments"]
            assert bot.store.get(tid)["state"] == "collecting" and bot.queue.empty()
            assert any("未找到" in text for text in bot.media.sent)
        finally:
            bot.store.close()
    asyncio.run(check())


def test_followup_of_collecting_task_prompts_start_instead_of_new_job(tmp_path):
    async def check():
        bot = make_bot(tmp_path)
        try:
            await handle(bot, "/收集 原题文字", "create")
            tid = bot.store.collecting("u")["task_id"]
            await handle(bot, f"/追问 {tid} 请解释", "premature-followup")
            assert bot.queue.empty() and len(bot.store.list_recent("u")) == 1
            assert f"/开始 {tid}" in bot.media.sent[-1]
        finally:
            bot.store.close()
    asyncio.run(check())


def test_append_photo_command_rejects_completed_task(tmp_path, monkeypatch):
    async def check():
        bot = make_bot(tmp_path)
        try:
            calls = mock_cache_download(monkeypatch)
            task, _ = bot.store.create("u", "parent", "c2c", "u", {"text": "原题"})
            bot.store.update(task["task_id"], state="completed", problem="原题文字")
            await handle(bot, f"/补图 {task['task_id']}", "append-to-completed", [attachment(1)])
            assert not calls and bot.queue.empty()
            assert len(bot.store.list_recent("u")) == 1
            assert any(word in bot.media.sent[-1] for word in ("收集", "补充"))
        finally:
            bot.store.close()
    asyncio.run(check())


def test_collecting_task_can_be_cancelled_without_starting_model(tmp_path):
    async def check():
        bot = make_bot(tmp_path)
        try:
            await handle(bot, "/收集 原题文字", "create")
            tid = bot.store.collecting("u")["task_id"]
            await handle(bot, f"/取消 {tid}", "cancel")
            assert bot.store.get(tid)["state"] == "cancelled" and bot.queue.empty()
            await handle(bot, f"/开始 {tid}", "start-cancelled")
            assert bot.queue.empty()
            await handle(bot, "/收集 另一道题", "another-draft")
            assert bot.store.collecting("u")["task_id"] != tid
        finally:
            bot.store.close()
    asyncio.run(check())


def test_help_describes_collection_and_image_supplements():
    assert all(command in HELP for command in ("/收集", "/补图", "/开始", "/补充"))
    assert "12" in HELP
    assert "追加图片时请重新发送完整题目和图片" not in HELP
