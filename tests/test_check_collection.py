import asyncio
import time

from test_collection_commands import (attachment, handle, make_bot,
                                     mock_cache_download)
from task_bot import ReplyContext


class RolesAI:
    def __init__(self, roles):
        self.roles = list(roles)
        self.calls = []

    async def classify_check_materials(self, images):
        self.calls.append(len(images))
        return {"roles": self.roles[:len(images)]}


def urls(items):
    return [item["url"] for item in items]


def test_collected_materials_are_classified_and_split_for_checking(tmp_path, monkeypatch):
    async def check():
        bot = make_bot(tmp_path)
        bot.ai = RolesAI(["question", "draft", "both"])
        try:
            mock_cache_download(monkeypatch)
            await handle(bot, "/收集", "create")
            tid = bot.store.collecting("u")["task_id"]
            await handle(bot, f"/补图 {tid}", "one", [attachment(1)])
            await handle(bot, f"/补图 {tid}", "two", [attachment(2)])
            await handle(bot, f"/补图 {tid}", "three", [attachment(3)])
            await handle(bot, f"/查错 {tid}", "check")

            assert bot.ai.calls == [3]
            task = bot.store.get(tid)
            assert task["state"] == "queued"
            assert bot.queue.get_nowait() == tid
            request = task["request"]
            assert request["action"] == "查错"
            assert urls(request["question_attachments"]) == [attachment(1)["url"], attachment(3)["url"]]
            assert urls(request["attachments"]) == [attachment(2)["url"], attachment(3)["url"]]
            assert "题目第 1、3 份" in bot.media.sent[-1]
            assert "草稿第 2、3 份" in bot.media.sent[-1]
        finally:
            bot.store.close()
    asyncio.run(check())


def test_manual_role_spec_skips_automatic_classification(tmp_path, monkeypatch):
    async def check():
        bot = make_bot(tmp_path)
        bot.ai = RolesAI(["unknown"])
        try:
            mock_cache_download(monkeypatch)
            await handle(bot, "/收集", "create")
            tid = bot.store.collecting("u")["task_id"]
            for number in (1, 2, 3):
                await handle(bot, f"/补图 {tid}", f"m{number}", [attachment(number)])
            await handle(bot, f"/查错 {tid} 1-2题 3稿", "check")

            assert bot.ai.calls == []
            request = bot.store.get(tid)["request"]
            assert urls(request["question_attachments"]) == [attachment(1)["url"], attachment(2)["url"]]
            assert urls(request["attachments"]) == [attachment(3)["url"]]
        finally:
            bot.store.close()
    asyncio.run(check())


def test_uncertain_auto_roles_keep_the_collection_and_ask_for_a_spec(tmp_path, monkeypatch):
    async def check():
        bot = make_bot(tmp_path)
        bot.ai = RolesAI(["question", "unknown"])
        try:
            mock_cache_download(monkeypatch)
            await handle(bot, "/收集", "create")
            tid = bot.store.collecting("u")["task_id"]
            for number in (1, 2):
                await handle(bot, f"/补图 {tid}", f"u{number}", [attachment(number)])
            await handle(bot, f"/查错 {tid}", "check")

            assert bot.queue.empty()
            assert bot.store.get(tid)["state"] == "collecting"
            assert "无法判断第 2 份" in bot.media.sent[-1]
        finally:
            bot.store.close()
    asyncio.run(check())


def test_collection_linked_to_a_question_uses_its_problem_and_all_materials_as_drafts(
        tmp_path, monkeypatch):
    async def check():
        bot = make_bot(tmp_path)
        bot.ai = RolesAI([])
        try:
            mock_cache_download(monkeypatch)
            parent, _ = bot.store.create("u", "parent", "c2c", "u", {"text": "原题"})
            bot.store.update(parent["task_id"], state="completed", problem="求 x^2=4 的实根。")
            await handle(bot, f"/收集 {parent['task_id']}", "create")
            tid = bot.store.collecting("u")["task_id"]
            await handle(bot, f"/补图 {tid}", "draft", [attachment(1)])
            await handle(bot, f"/查错 {tid}", "check")

            assert bot.ai.calls == []
            request = bot.store.get(tid)["request"]
            assert request["problem_snapshot"] == "求 x^2=4 的实根。"
            assert request["origin_id"] == parent["task_id"]
            assert request["question_attachments"] == []
            assert urls(request["attachments"]) == [attachment(1)["url"]]
        finally:
            bot.store.close()
    asyncio.run(check())


def test_single_collected_material_can_be_checked_as_same_page(tmp_path, monkeypatch):
    async def check():
        bot = make_bot(tmp_path)
        bot.ai = RolesAI(["question"])
        try:
            mock_cache_download(monkeypatch)
            await handle(bot, "/收集", "create")
            tid = bot.store.collecting("u")["task_id"]
            await handle(bot, f"/补图 {tid}", "one", [attachment(1)])
            await handle(bot, f"/查错 {tid} 同页", "check")

            assert bot.ai.calls == []
            request = bot.store.get(tid)["request"]
            assert urls(request["question_attachments"]) == [attachment(1)["url"]]
            assert urls(request["attachments"]) == [attachment(1)["url"]]
        finally:
            bot.store.close()
    asyncio.run(check())


def test_single_image_with_typed_draft_treats_the_image_as_the_question(tmp_path, monkeypatch):
    async def check():
        bot = make_bot(tmp_path)
        try:
            mock_cache_download(monkeypatch)
            await handle(bot, "/查错 我令 t=x", "one-shot", [attachment(1)])
            request = bot.store.get(bot.queue.get_nowait())["request"]
            assert urls(request["question_attachments"]) == [attachment(1)["url"]]
            assert request["attachments"] == []
            assert request["text"] == "我令 t=x"
        finally:
            bot.store.close()
    asyncio.run(check())


def test_solve_command_starts_a_collection(tmp_path, monkeypatch):
    async def check():
        bot = make_bot(tmp_path)
        try:
            mock_cache_download(monkeypatch)
            await handle(bot, "/收集", "create", [attachment(1)])
            tid = bot.store.collecting("u")["task_id"]
            await handle(bot, f"/解题 {tid}", "solve")
            assert bot.store.get(tid)["state"] == "queued"
            assert bot.queue.get_nowait() == tid
        finally:
            bot.store.close()
    asyncio.run(check())


def test_cancel_also_stops_active_tasks_derived_from_the_same_question(tmp_path):
    async def check():
        bot = make_bot(tmp_path)
        try:
            parent, _ = bot.store.create("u", "parent", "c2c", "u", {"text": "原题"},
                                         max_user_active=10)
            bot.store.update(parent["task_id"], state="solving")
            child, _ = bot.store.create("u", "child", "c2c", "u",
                                        {"text": "追问", "parent_id": parent["task_id"]},
                                        max_user_active=10)
            retry, _ = bot.store.create("u", "retry", "c2c", "u",
                                        {"text": "重试", "retry_of": parent["task_id"]},
                                        max_user_active=10)
            context = ReplyContext("c2c", "u", "u", "cancel", time.time())
            await bot._cancel_task(context, bot.store.get(parent["task_id"]))
            assert bot.store.get(child["task_id"])["state"] == "cancelled"
            assert bot.store.get(retry["task_id"])["state"] == "cancelled"
            assert "已取消" in bot.media.sent[-1]
        finally:
            bot.store.close()
    asyncio.run(check())


def test_cancel_without_an_id_cancels_the_latest_collection(tmp_path, monkeypatch):
    async def check():
        bot = make_bot(tmp_path)
        try:
            mock_cache_download(monkeypatch)
            await handle(bot, "/收集", "create", [attachment(1)])
            tid = bot.store.collecting("u")["task_id"]
            await handle(bot, "/取消", "cancel")
            assert bot.store.get(tid)["state"] == "cancelled"
        finally:
            bot.store.close()
    asyncio.run(check())
