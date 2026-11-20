import asyncio
from collections import OrderedDict
from pathlib import Path
import re
import time
from types import SimpleNamespace

import pytest

from task_bot import HELP, ReplyContext, TaskBot
from task_delivery import _split_text_reply
from task_store import TaskStore


class FakeMedia:
    def __init__(self):
        self.sent = []
        self.uploaded = []
        self.fail_image = False
        self.fail_file = False

    async def upload_image(self, kind, target, data, filename):
        self.uploaded.append(("image", filename))
        return filename

    async def upload_file_path(self, kind, target, path):
        assert Path(path).is_file()
        self.uploaded.append(("file", Path(path).name))
        return Path(path).name

    async def send_text(self, kind, target, text, msg_id=None, msg_seq=None):
        self.sent.append(("text", text, msg_id, msg_seq))

    async def send_image(self, kind, target, info, msg_id=None, msg_seq=None):
        if self.fail_image:
            raise RuntimeError("synthetic image delivery failure")
        self.sent.append(("image", info, msg_id, msg_seq))

    async def send_file(self, kind, target, info, msg_id=None, msg_seq=None):
        if self.fail_file:
            raise RuntimeError("synthetic file delivery failure")
        self.sent.append(("file", info, msg_id, msg_seq))


def make_bot(tmp_path):
    bot = object.__new__(TaskBot)
    bot.store = TaskStore(tmp_path)
    bot.media = FakeMedia()
    bot.settings = SimpleNamespace(
        active_delivery=False, job_timeout=1200, result_ttl=7, disk_limit_mb=512,
        max_queue=5, max_user_tasks=2, max_daily=50)
    bot.resource_lock = asyncio.Lock()
    bot.queue = asyncio.Queue(maxsize=5)
    bot.contexts = {}
    bot.seen = OrderedDict()
    bot.running_task = bot.running_id = None
    bot.book_admin_openid = ""
    bot._ensure_worker = lambda: None
    return bot


def saved_answer(bot, pages=3, owner="u"):
    task, _ = bot.store.create(owner, "original", "c2c", "u", {"text": "求解原题"})
    tid = task["task_id"]
    directory = bot.store.task_dir(tid)
    pdf, text = directory / f"{tid}.pdf", directory / f"{tid}.txt"
    pdf.write_bytes(b"%PDF-1.4 synthetic saved answer")
    text.write_text("完整文字解答", encoding="utf-8")
    previews = []
    for page in range(1, pages + 1):
        path = directory / f"preview-{page:03d}.png"
        path.write_bytes(f"synthetic page {page}".encode("ascii"))
        previews.append(str(path))
    bot.store.update(tid, state="completed", problem="求解原题", result={
        "pdf_path": str(pdf), "text_path": str(text), "total_pages": pages,
        "preview_paths": previews, "verification": "模型复核通过（不等于严格正确性保证）"})
    return bot.store.get(tid)


def context(kind="c2c", seq=0, received=None):
    return ReplyContext(kind, "u", "u", "fresh-query",
                        time.time() if received is None else received, seq=seq)


@pytest.mark.parametrize("pages", [1, 2, 3, 4, 7])
def test_default_delivery_uses_all_short_pages_or_complete_pdf(tmp_path, pages):
    async def check():
        bot = make_bot(tmp_path)
        try:
            task = saved_answer(bot, pages)
            await bot._deliver(task, context())
            media = [item for item in bot.media.sent if item[0] != "text"]
            if pages <= 3:
                assert [(item[0], item[1]) for item in media] == [
                    ("image", f"preview-{page:03d}.png") for page in range(1, pages + 1)]
            else:
                assert [(item[0], item[1]) for item in media] == [("file", f"{task['task_id']}.pdf")]
            assert bot.store.get(task["task_id"])["delivery"] == "delivered"
        finally:
            bot.store.close()
    asyncio.run(check())


@pytest.mark.parametrize("kind,seq,types,sequence", [
    ("c2c", 1, ["image"] * 3, [2, 3, 4]),
    ("c2c", 0, ["text"] + ["image"] * 3, [1, 2, 3, 4]),
    ("group", 1, ["text"] + ["image"] * 3, [2, 3, 4, 5]),
])
def test_three_page_delivery_fits_existing_reply_budget(tmp_path, kind, seq, types, sequence):
    async def check():
        bot = make_bot(tmp_path)
        try:
            task = saved_answer(bot)
            await bot._deliver(task, context(kind, seq))
            assert [item[0] for item in bot.media.sent] == types
            assert [item[3] for item in bot.media.sent] == sequence
            assert bot.store.get(task["task_id"])["delivery"] == "delivered"
        finally:
            bot.store.close()
    asyncio.run(check())


def test_long_status_does_not_consume_result_media_slots(tmp_path):
    async def check():
        bot = make_bot(tmp_path)
        try:
            task = saved_answer(bot, pages=3)
            result = task["result"]
            result["notes"] = "N" * 2400
            bot.store.update(task["task_id"], result=result)
            await bot._deliver(bot.store.get(task["task_id"]), context("c2c", 0))
            assert [item[0] for item in bot.media.sent if item[0] != "text"] == ["image"] * 3
            assert bot.store.get(task["task_id"])["delivery"] == "delivered"
            texts = [item[1] for item in bot.media.sent if item[0] == "text"]
            assert len(texts) == 1 and len(texts[0]) <= 1800
        finally:
            bot.store.close()
    asyncio.run(check())


def test_text_reply_split_marks_unfinished_tail():
    parts, truncated = _split_text_reply("x" * 4000, 2)
    assert len(parts) == 2 and truncated
    assert parts[-1].endswith("… 本次未完整发送，请缩短请求或分批提交材料。")


@pytest.mark.parametrize("prefix,is_pdf", [
    ("", False), ("/结果 ", False), ("结果 ", False),
    ("/PDF ", True), ("/pdf ", True), ("／ PDF ", True), ("PDF ", True),
])
def test_result_and_pdf_commands_route_without_new_jobs(tmp_path, prefix, is_pdf):
    async def check():
        bot = make_bot(tmp_path)
        try:
            task = saved_answer(bot)
            message = SimpleNamespace(id="query", content=prefix + task["task_id"].lower())
            await bot._handle(message, "c2c", "u", "u")
            media = [item for item in bot.media.sent if item[0] != "text"]
            assert [item[0] for item in media] == (["file"] if is_pdf else ["image"] * 3)
            assert bot.queue.empty()
            assert len(bot.store.list_recent("u")) == 1
        finally:
            bot.store.close()
    asyncio.run(check())


@pytest.mark.parametrize("mode", ["结果", "PDF"])
@pytest.mark.parametrize("missing_file", [False, True])
def test_missing_pdf_is_reported_without_uploading_text_as_pdf(tmp_path, mode, missing_file):
    async def check():
        bot = make_bot(tmp_path)
        try:
            task = saved_answer(bot)
            if missing_file:
                Path(task["result"]["pdf_path"]).unlink()
            else:
                result = task["result"]
                result.pop("pdf_path")
                bot.store.update(task["task_id"], result=result)
            await bot._deliver(bot.store.get(task["task_id"]), context(), mode)
            assert not bot.media.uploaded
            assert all(item[0] == "text" for item in bot.media.sent)
            reply = bot.media.sent[0][1]
            if mode == "PDF":
                assert "暂无 PDF" in reply
                assert bot.store.get(task["task_id"])["delivery"] == "awaiting_query"
            else:
                assert "完整文字解答" in reply
                assert bot.store.get(task["task_id"])["delivery"] == "delivered"
            assert Path(task["result"]["text_path"]).is_file()
        finally:
            bot.store.close()
    asyncio.run(check())


def test_result_pages_deliver_saved_text_without_creating_a_followup(tmp_path):
    async def check():
        bot = make_bot(tmp_path)
        try:
            task = saved_answer(bot)
            tid = task["task_id"]
            result = task["result"]
            Path(result["pdf_path"]).unlink()
            Path(result["text_path"]).write_text(
                "开头的推导\n" + "甲" * 1450 + "\n末尾的结论", encoding="utf-8")
            result.update(answer_status="uncertain", verification="未完成（服务重启中断）")
            bot.store.update(tid, result=result)
            for event, text in enumerate((f"/结果 {tid}", f"/结果 {tid} 第2段", f"/结果 {tid} 第99段")):
                await bot._handle(SimpleNamespace(id=str(event), content=text), "c2c", "u", "u")
            first, second, invalid = (item[1] for item in bot.media.sent)
            assert "开头的推导" in first and f"/结果 {tid} 第2段" in first
            assert "末尾的结论" in second and "开头的推导" not in second
            assert "未完成（服务重启中断）" in first and "未完成（服务重启中断）" in second
            assert "没有第 99 段" in invalid
            assert bot.queue.empty()
            assert len(bot.store.list_recent("u")) == 1
        finally:
            bot.store.close()

    asyncio.run(check())


@pytest.mark.parametrize("page,expected", [(1, [1, 2]), (3, [3, 4]), (4, [4])])
def test_explicit_images_keep_two_page_batches(tmp_path, page, expected):
    async def check():
        bot = make_bot(tmp_path)
        try:
            task = saved_answer(bot, 4)
            await bot._deliver(task, context(), "图片", page)
            assert bot.media.uploaded == [("image", f"preview-{number:03d}.png") for number in expected]
        finally:
            bot.store.close()
    asyncio.run(check())


def test_image_page_out_of_range_is_explained_without_upload(tmp_path):
    async def check():
        bot = make_bot(tmp_path)
        try:
            task = saved_answer(bot, 4)
            await bot._deliver(task, context(), "图片", 5)
            assert not bot.media.uploaded
            assert any("页" in item[1] and any(word in item[1] for word in ("没有", "超出", "无效", "范围"))
                       for item in bot.media.sent if item[0] == "text")
        finally:
            bot.store.close()
    asyncio.run(check())


@pytest.mark.parametrize("pages", [2, 4])
def test_send_failure_keeps_saved_answer_for_another_query(tmp_path, pages):
    async def check():
        bot = make_bot(tmp_path)
        try:
            task = saved_answer(bot, pages)
            bot.media.fail_image = pages <= 3
            bot.media.fail_file = pages > 3
            await bot._deliver(task, context())
            row = bot.store.get(task["task_id"])
            assert row["state"] == "completed" and row["delivery"] == "awaiting_query"
            assert Path(row["result"]["pdf_path"]).is_file()
            assert Path(row["result"]["text_path"]).is_file()
            assert any("结果已保存" in item[1] for item in bot.media.sent if item[0] == "text")
        finally:
            bot.store.close()
    asyncio.run(check())


def test_expired_reply_window_does_not_upload_or_mark_delivered(tmp_path):
    async def check():
        bot = make_bot(tmp_path)
        try:
            task = saved_answer(bot)
            await bot._deliver(task, context(received=time.time() - 4000))
            assert not bot.media.uploaded
            assert not bot.media.sent
            assert bot.store.get(task["task_id"])["delivery"] == "awaiting_query"
        finally:
            bot.store.close()
    asyncio.run(check())


def test_other_user_cannot_retrieve_pdf(tmp_path):
    async def check():
        bot = make_bot(tmp_path)
        try:
            task = saved_answer(bot, owner="original-owner")
            await bot._handle(SimpleNamespace(id="query", content=f"/PDF {task['task_id']}"),
                              "c2c", "u", "another-owner")
            assert not bot.media.uploaded
            assert any("未找到" in item[1] for item in bot.media.sent)
            assert bot.queue.empty()
        finally:
            bot.store.close()
    asyncio.run(check())


def test_pdf_extra_arguments_show_usage_without_creating_a_job(tmp_path):
    async def check():
        bot = make_bot(tmp_path)
        try:
            task = saved_answer(bot)
            await bot._handle(SimpleNamespace(id="query", content=f"/PDF {task['task_id']} 2"),
                              "c2c", "u", "u")
            assert not bot.media.uploaded and bot.queue.empty()
            assert len(bot.store.list_recent("u")) == 1
            assert any("/PDF" in item[1] for item in bot.media.sent)
        finally:
            bot.store.close()
    asyncio.run(check())


@pytest.mark.parametrize("action", ["new", "followup", "retry"])
def test_registration_is_one_short_receipt_without_command_spam(tmp_path, action):
    async def check():
        bot = make_bot(tmp_path)
        try:
            parent = saved_answer(bot)
            content = {"new": "求积分 x^2", "followup": f"/追问 {parent['task_id']} 请解释第三步",
                       "retry": f"/重试 {parent['task_id']}"}[action]
            await bot._handle(SimpleNamespace(id=f"request-{action}", content=content), "c2c", "u", "u")
            receipts = [item[1] for item in bot.media.sent if item[0] == "text" and item[1].startswith("已登记")]
            assert len(receipts) == 1
            assert re.fullmatch(r"已登记 Q\d{6}-\d{6}。\n正在解题，请稍候…", receipts[0])
            assert all(command not in receipts[0] for command in
                       ("/帮助", "/PDF", "/进度", "/图片", "/追问", "/重试"))
            assert bot.queue.qsize() == 1
        finally:
            bot.store.close()
    asyncio.run(check())


def test_registration_ack_offers_a_task_id_button(tmp_path):
    async def check():
        bot = make_bot(tmp_path)
        calls = []

        async def send_command_choices(kind, target, text, choices, msg_id=None, msg_seq=None,
                                       per_row=1):
            calls.append((text, choices))

        bot.media.send_command_choices = send_command_choices
        try:
            await bot._handle(SimpleNamespace(id="button-ack", content="求积分 x^2"), "c2c", "u", "u")
            tid = bot.store.list_recent("u")[0]["task_id"]
            assert len(calls) == 1
            text, choices = calls[0]
            assert text == f"已登记 {tid}。\n正在解题，请稍候…"
            assert choices == [(tid, tid)]
            assert bot.queue.qsize() == 1
        finally:
            bot.store.close()
    asyncio.run(check())


def test_command_buttons_wrap_three_per_row():
    from qqmedia import QQMedia

    async def check():
        media = QQMedia("app", "plain-secret", active_delivery=False)
        captured = {}

        async def payload(kind, target, data, msg_id, msg_seq):
            captured.update(data)

        media._send_payload = payload
        try:
            await media.send_command_choices(
                "c2c", "u", "text",
                [(f"L{index}", f"/cmd {index}") for index in range(5)], per_row=3)
        finally:
            await media.close()
        rows = captured["keyboard"]["content"]["rows"]
        assert [len(row["buttons"]) for row in rows] == [3, 2]
        assert rows[1]["buttons"][0]["action"]["data"] == "/cmd 3"
    asyncio.run(check())


def test_help_feedback_contains_main_commands(tmp_path):
    async def check():
        bot = make_bot(tmp_path)
        try:
            await bot._handle(SimpleNamespace(id="help", content="/帮助"), "c2c", "u", "u")
            assert len(bot.media.sent) == 1 and bot.media.sent[0][0] == "text"
            assert bot.media.sent[0][1] == HELP
            assert len(HELP) <= 1800
            for command in ("/结果", "/PDF", "/进度", "/图片", "/查错", "/追问", "/补充", "/取消", "/重试", "/题目", "/帮助"):
                assert command in HELP
            assert bot.queue.empty()
        finally:
            bot.store.close()
    asyncio.run(check())


@pytest.mark.parametrize("pages", [3, 4])
def test_worker_automatic_delivery_uses_the_same_page_rule(tmp_path, pages):
    async def check():
        bot = make_bot(tmp_path)
        worker = None
        try:
            task = saved_answer(bot, pages)
            tid = task["task_id"]
            bot.store.update(tid, state="queued")
            async def solve(row):
                bot.store.update(row["task_id"], state="completed")
            bot.solver = SimpleNamespace(run=solve)
            bot.contexts[tid] = context(seq=1)
            bot.queue.put_nowait(tid)
            worker = asyncio.create_task(bot._worker())
            await asyncio.wait_for(bot.queue.join(), 2)
            assert bot.store.get(tid)["delivery"] == "delivered"
            media = [item[0] for item in bot.media.sent if item[0] != "text"]
            assert media == (["image"] * 3 if pages == 3 else ["file"])
        finally:
            if worker is not None:
                worker.cancel()
                try:
                    await worker
                except asyncio.CancelledError:
                    pass
            bot.store.close()
    asyncio.run(check())


@pytest.mark.parametrize("pages", [1, 2, 3, 4])
def test_legacy_result_without_page_count_recovers_metadata_and_delivery_mode(tmp_path, monkeypatch, pages):
    async def check():
        bot = make_bot(tmp_path)
        try:
            task = saved_answer(bot, pages)
            tid = task["task_id"]
            result = task["result"]
            result.pop("total_pages")
            result.pop("preview_paths")
            bot.store.update(tid, result=result)
            calls = []
            expected_previews = [str(bot.store.task_dir(tid) / f"preview-{page:03d}.png")
                                 for page in range(1, min(pages, 3) + 1)]

            async def recover(pdf_path, directory, start_page, count):
                calls.append((pdf_path, directory, start_page, count))
                return {"total_pages": pages, "preview_paths": expected_previews}

            monkeypatch.setattr("task_delivery.render_preview_async", recover)
            await bot._deliver(bot.store.get(tid), context())
            assert calls == [(Path(result["pdf_path"]), bot.store.task_dir(tid), 1, 3)]
            row = bot.store.get(tid)
            assert row["result"]["total_pages"] == pages
            assert row["result"]["preview_paths"] == expected_previews
            assert row["delivery"] == "delivered"
            media = [item[0] for item in bot.media.sent if item[0] != "text"]
            assert media == (["image"] * pages if pages <= 3 else ["file"])
        finally:
            bot.store.close()
    asyncio.run(check())


def test_legacy_three_page_result_regenerates_missing_third_preview(tmp_path, monkeypatch):
    async def check():
        bot = make_bot(tmp_path)
        try:
            task = saved_answer(bot)
            tid = task["task_id"]
            third = bot.store.task_dir(tid) / "preview-003.png"
            third.unlink()
            result = task["result"]
            result["preview_paths"] = result["preview_paths"][:2]
            bot.store.update(tid, result=result)
            calls = []

            async def regenerate(pdf_path, directory, start_page, count):
                calls.append((pdf_path, directory, start_page, count))
                third.write_bytes(b"regenerated third page")
                return {"total_pages": 3, "preview_paths": [
                    str(directory / f"preview-{page:03d}.png") for page in range(1, 4)]}

            monkeypatch.setattr("task_delivery.render_preview_async", regenerate)
            await bot._deliver(bot.store.get(tid), context(seq=1))
            assert calls == [(Path(result["pdf_path"]), bot.store.task_dir(tid), 1, 3)]
            assert third.read_bytes() == b"regenerated third page"
            assert bot.media.uploaded == [("image", f"preview-{page:03d}.png") for page in range(1, 4)]
            assert bot.store.get(tid)["delivery"] == "delivered"
        finally:
            bot.store.close()
    asyncio.run(check())


def test_legacy_missing_preview_waits_for_render_lock_and_offers_pdf(tmp_path, monkeypatch):
    async def check():
        bot = make_bot(tmp_path)
        try:
            task = saved_answer(bot)
            tid = task["task_id"]
            third = bot.store.task_dir(tid) / "preview-003.png"
            third.unlink()
            result = task["result"]
            result["preview_paths"] = result["preview_paths"][:2]
            bot.store.update(tid, result=result)
            calls = []

            async def should_not_render(*args):
                calls.append(args)
                raise AssertionError("another task owns the rendering lock")

            monkeypatch.setattr("task_delivery.render_preview_async", should_not_render)
            await bot.resource_lock.acquire()
            try:
                await bot._deliver(bot.store.get(tid), context())
            finally:
                bot.resource_lock.release()
            assert not calls and not bot.media.uploaded
            assert bot.store.get(tid)["delivery"] == "awaiting_query"
            assert not third.exists()
            assert any(f"/PDF {tid}" in item[1] for item in bot.media.sent if item[0] == "text")
        finally:
            bot.store.close()
    asyncio.run(check())
