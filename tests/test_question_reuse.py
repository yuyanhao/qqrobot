import asyncio
from hashlib import sha256
import json

import pytest

from task_store import TaskStore
from test_study_commands import OWNER, PROBLEM, StudyChat, photo, solution


def fake_model(prompts, *, note="", confirm=False):
    async def chat(messages, *args):
        system, content = messages[0]["content"], messages[-1]["content"]
        prompts.append(content)
        if isinstance(content, list):
            return json.dumps({"problem_latex": PROBLEM, "notes": note, "needs_confirmation": confirm},
                              ensure_ascii=False)
        if "规划器" in system:
            return '{"accepted": true, "domain": "math", "needs_search": false, "search_query": ""}'
        if "A4 讲义" in system:
            return json.dumps(solution(), ensure_ascii=False)
        return '{"status":"complete","feedback":"先移项。"}'

    return chat


async def no_pdf(*args):
    return {}


def test_ocr_note_reaches_check_and_question_display_is_owner_scoped(tmp_path, monkeypatch):
    prompts = []
    note = "行末略模糊，现有条件已读清"
    monkeypatch.setattr("solver.render_artifacts_async", no_pdf)

    async def check():
        session = StudyChat(tmp_path, fake_model(prompts, note=note))
        try:
            await session.send("", attachments=photo(monkeypatch))
            first = session.latest()["task_id"]
            display = await session.send(f"/题干 {first}")
            assert PROBLEM in display and note in display
            assert note not in await session.send(f"/题干 {first}", owner="group:g:other")
            await session.send(f"/查错 {first} 两边取平方根")
            assert note in prompts[-1]
            assert sum(isinstance(message, list) for message in prompts) == 1
        finally:
            session.bot.store.close()

    asyncio.run(check())


@pytest.mark.parametrize("check_mode", [False, True])
def test_critical_ocr_waits_for_confirmation_then_resumes_the_same_mode(tmp_path, monkeypatch, check_mode):
    prompts = []
    note = "上标可能是 2 或 3"
    monkeypatch.setattr("solver.render_artifacts_async", no_pdf)

    async def check():
        session = StudyChat(tmp_path, fake_model(prompts, note=note, confirm=True))
        try:
            attachments = photo(monkeypatch)
            if check_mode:
                attachments.append({**attachments[0], "url": "https://example.com/draft.png"})
            reply = await session.send("/查错" if check_mode else "", attachments=attachments)
            pending = session.latest()
            tid = pending["task_id"]
            assert pending["state"] == "needs_input" and len(prompts) == 1
            assert f"/题干 {tid}" in reply and note in await session.send(f"/题干 {tid}")
            assert f"/确认题干 {tid}" in await session.send(f"/查错 {tid} 草稿")
            reply = await session.send(f"/确认题干 {tid} 指数应是 3。")
            continued = session.latest()
            assert continued["task_id"] != tid and continued["state"] == "completed"
            assert continued["request"].get("action") == ("查错" if check_mode else None)
            assert "指数应是 3" in prompts[-1]
            assert len(prompts) == 3
            assert continued["task_id"] in await session.send(f"/确认题干 {tid} 指数应是 3。")
            assert len(prompts) == 3
            assert note in await session.send(f"/题干 {continued['task_id']}")
            assert "已由你确认" in await session.send(f"/题干 {continued['task_id']}")
            for command in ("/结果", "/进度", "/题干", "/查错"):
                reply = await session.send(f"{command} {tid}")
                assert continued["task_id"] in reply and "已校对" in reply
            assert tid not in await session.send("/确认题干")
            assert continued["task_id"] in await session.send(f"/确认题干 {tid}")
            assert len(prompts) == 3
        finally:
            session.bot.store.close()

    asyncio.run(check())


def test_confirmation_link_is_recovered_for_older_saved_requests(tmp_path):
    store = TaskStore(tmp_path)
    original, _ = store.create(OWNER, "old-image", "group", "g", {})
    tid = original["task_id"]
    ocr = {"notes": ["上标模糊"], "needs_confirmation": True, "mode": "解题"}
    store.update(tid, state="needs_input", problem=PROBLEM, question_ocr=ocr)
    confirmed = {**ocr, "needs_confirmation": False, "confirmed": True}
    corrected = PROBLEM + "\n\n用户核对后的更正（以本段为准）：\n右侧为 9"
    revision = json.dumps([tid, corrected, confirmed], ensure_ascii=False, sort_keys=True)
    key = "confirm:" + sha256(revision.encode("utf-8")).hexdigest()
    continued, _ = store.create(OWNER, "old-confirm", "group", "g", {
        "text": corrected, "question_ocr": confirmed, "reuse_key": key,
    })
    store.update(continued["task_id"], state="completed", problem=corrected, question_ocr=confirmed)
    store.close()

    session = StudyChat(tmp_path, fake_model([]))

    async def check():
        reply = await session.send(f"/查错 {tid} 我已解出平方根")
        assert continued["task_id"] in reply and "已校对" in reply
        other = await session.send(f"/结果 {tid}", owner="group:g:other")
        assert continued["task_id"] not in other

    try:
        asyncio.run(check())
    finally:
        session.bot.store.close()

