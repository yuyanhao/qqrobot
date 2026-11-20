import asyncio
from datetime import datetime, timezone
import time
from types import SimpleNamespace

from qqrobot.media.qqmedia import QQMedia, QQMediaError
from test_delivery_modes import make_bot


OWNER = "group:g:alice"


def add_task(store, message, *, owner=OWNER, state="completed", ocr=None):
    target = owner.split(":")[1]
    task, _ = store.create(owner, message, "group", target, {"text": "不应回显的题干"})
    tid = task["task_id"]
    store.update(tid, state=state, problem="不应回显的题干", question_ocr=ocr or {})
    return tid


async def dispatch(bot, text, *, owner=OWNER, event="message"):
    message = SimpleNamespace(id=event, content=f"<@!12345> {text}", attachments=[])
    await bot._handle(message, "group", owner.split(":")[1], owner)


def test_picker_fills_only_the_latest_three_eligible_task_ids_for_this_member_and_group(tmp_path):
    bot = make_bot(tmp_path)
    try:
        older = add_task(bot.store, "older")
        ids = [add_task(bot.store, str(index)) for index in range(3)]
        expired = add_task(bot.store, "expired", state="expired")
        other_user = add_task(bot.store, "other-user", owner="group:g:bob")
        other_group = add_task(bot.store, "other-group", owner="group:other:alice")
        calls = []
        media = QQMedia("app", "fake-secret", active_delivery=False)
        bot.media = media

        async def request(path, payload):
            calls.append((path, payload))
            return {"id": "reply"}

        media._request = request
        asyncio.run(dispatch(bot, "／ pdf"))
        assert len(calls) == 1
        path, payload = calls[0]
        assert path == "/v2/groups/g/messages" and payload["msg_type"] == 2
        assert "markdown" in payload and "不应回显的题干" not in payload["markdown"]["content"]
        assert payload["msg_id"] == "message" and payload["msg_seq"] == 1
        buttons = [row["buttons"][0] for row in payload["keyboard"]["content"]["rows"]]
        assert [button["action"]["data"] for button in buttons] == [f"/PDF {tid}" for tid in reversed(ids)]
        assert all(button["action"]["type"] == 2 and button["action"]["enter"] is False for button in buttons)
        assert all(tid not in str(payload) for tid in (older, expired, other_user, other_group))
        assert "不应回显的题干" not in str(payload)
        assert bot.queue.empty()
    finally:
        bot.store.close()


def test_picker_falls_back_to_copyable_commands_on_qq_permission_error(tmp_path):
    bot = make_bot(tmp_path)
    try:
        tid = add_task(bot.store, "own")
        attempts = []

        async def refuse(kind, target, text, choices, msg_id, msg_seq, per_row=1):
            attempts.append((msg_id, msg_seq))
            raise QQMediaError("模拟没有自定义按钮权限", category="permission", status=403)

        bot.media.send_command_choices = refuse
        asyncio.run(dispatch(bot, "/查错 最近"))
        assert attempts == [("message", 1)]
        _, reply, msg_id, seq = bot.media.sent[0]
        assert f"\n/收集 {tid}" in reply and "草稿" in reply
        assert (msg_id, seq) == ("message", 2)
        assert bot.queue.empty()
    finally:
        bot.store.close()


def test_check_picker_skips_unread_questions_and_shows_submission_time(tmp_path, monkeypatch):
    now = datetime(2026, 1, 2, 7, 42, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr("qqrobot.tasks.task_store.time.time", lambda: now)
    bot = make_bot(tmp_path)
    try:
        ready = [add_task(bot.store, f"ready-{i}") for i in range(3)]
        unread = add_task(bot.store, "unread", state="queued")
        bot.store.update(unread, problem="")

        asyncio.run(dispatch(bot, "/查错 最近"))

        reply = bot.media.sent[0][1]
        assert [line for line in reply.splitlines() if line.startswith("/收集 ")] == [
            f"/收集 {tid}" for tid in reversed(ready)]
        assert "01-02 15:42" in reply
        assert unread not in reply
        assert "不应回显的题干" not in reply
    finally:
        bot.store.close()


def test_explicit_question_id_still_executes_the_original_command(tmp_path):
    bot = make_bot(tmp_path)
    try:
        tid = add_task(bot.store, "own")
        asyncio.run(dispatch(bot, f"/进度 {tid}"))
        assert bot.media.sent[0][1].startswith(f"{tid} · ")
        assert "最近" not in bot.media.sent[0][1]
        asyncio.run(dispatch(bot, f"/进度 {tid}", owner="group:g:bob", event="foreign"))
        assert "未找到" in bot.media.sent[-1][1]
        assert tid not in bot.media.sent[-1][1]
    finally:
        bot.store.close()


def test_recent_buttons_fill_bare_question_id(tmp_path):
    bot = make_bot(tmp_path)
    try:
        tid = add_task(bot.store, "own")
        asyncio.run(dispatch(bot, "/最近"))
        assert bot.media.sent[0][1].endswith(tid)
        assert bot.queue.empty()
    finally:
        bot.store.close()


def test_recent_offers_four_bare_ids_in_two_rows_of_two(tmp_path):
    bot = make_bot(tmp_path)
    try:
        ids = [add_task(bot.store, f"recent-{index}") for index in range(5)]
        calls = []

        async def send_command_choices(kind, target, text, choices, msg_id=None, msg_seq=None,
                                       per_row=1):
            calls.append((text, choices, per_row))

        bot.media.send_command_choices = send_command_choices
        asyncio.run(dispatch(bot, "/最近"))
        text, choices, per_row = calls[0]
        newest = list(reversed(ids))[:4]
        assert text == '<qqbot-at-user id="alice" />'
        assert choices == [(tid, tid) for tid in newest]
        assert per_row == 2
    finally:
        bot.store.close()


def test_recent_excludes_due_tasks_without_waiting_for_cleanup(tmp_path):
    bot = make_bot(tmp_path)
    bot.settings.result_ttl = 3
    try:
        recent = add_task(bot.store, "recent")
        saved = add_task(bot.store, "saved")
        queued = add_task(bot.store, "queued", state="queued")
        due = add_task(bot.store, "due")
        with bot.store.db:
            bot.store.db.execute(
                "UPDATE tasks SET created=? WHERE task_id IN (?,?,?)",
                (time.time() - 4 * 86400, saved, queued, due),
            )

        asyncio.run(dispatch(bot, "/最近"))

        reply = bot.media.sent[0][1]
        assert [line for line in reply.splitlines() if line.startswith("Q")] == [queued, recent]
        assert bot.store.get(due)["state"] == "completed"
    finally:
        bot.store.close()
