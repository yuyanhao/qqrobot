import asyncio
from collections import OrderedDict
import io
import json
from types import SimpleNamespace

from PIL import Image
import pytest

from ai import AIError, DeepSeek
from settings import Settings
from solver import Solver
from task_bot import TaskBot
from task_store import TaskStore


OWNER = "group:g:learner"
PROBLEM = "求方程 x²=4 的实根。"
ANSWER = "完整解答：根为 x=2 或 x=-2。"


class StudyChat:
    def __init__(self, tmp_path, chat, *, sympy=False):
        self.replies = []
        bot = self.bot = object.__new__(TaskBot)
        bot.store = TaskStore(tmp_path)
        bot.settings = Settings(data_dir=tmp_path, search=False, verify=False, sympy=sympy)
        bot.ai = object.__new__(DeepSeek)
        bot.ai._chat = chat
        bot.ai.vision_model, bot.ai.solve_max_tokens = "fake-vision", 8192
        bot.media = SimpleNamespace(send_text=self.send_text)
        bot.solver = Solver(bot.store, bot.ai, bot.settings)
        bot.queue = asyncio.Queue(maxsize=bot.settings.max_queue)
        bot.contexts, bot.seen = {}, OrderedDict()
        bot.resource_lock = asyncio.Lock()
        bot.worker_task = bot.running_task = bot.running_id = None

    async def send_text(self, kind, target, text, msg_id, msg_seq):
        self.replies.append(text)

    def solved_question(self, *, problem=PROBLEM):
        task, _ = self.bot.store.create(OWNER, "original", "group", "g", {"text": problem})
        self.bot.store.update(task["task_id"], state="completed", problem=problem,
                              result={"solution": solution(problem)})
        return task["task_id"]

    async def send(self, content, *, attachments=(), owner=OWNER):
        first = len(self.replies)
        message = SimpleNamespace(id=f"msg-{len(self.bot.seen)}", content=content, attachments=attachments)
        await self.bot._handle(message, "group", owner.split(":")[1], owner)
        await asyncio.wait_for(self.bot.queue.join(), timeout=12)
        return "\n".join(self.replies[first:])

    def latest(self):
        return self.bot.store.list_recent(OWNER)[0]


def solution(problem=PROBLEM):
    return {"title": "方程求根", "problem_latex": problem, "steps": [{"text": "先移项", "math": "x^2-4=0"}],
            "answer_latex": ANSWER, "check_latex": "", "status": "complete", "plots": []}


def photo(monkeypatch):
    buffer = io.BytesIO()
    Image.new("RGB", (6, 6), "white").save(buffer, format="PNG")

    async def download(*args, **kwargs):
        return SimpleNamespace(data=buffer.getvalue())

    monkeypatch.setattr("solver.download_file", download)
    return [{"url": "https://example.com/input.png", "content_type": "image/png"}]


def test_study_and_favorites_include_the_question_from_an_existing_followup(tmp_path):
    followup = "为什么还要保留负根？"
    prompts = []

    async def chat(messages, *args):
        prompts.append(messages[-1]["content"])
        return '{"status":"complete","feedback":"把负根代回去试试。"}'

    async def check():
        session = StudyChat(tmp_path, chat)
        try:
            original = session.solved_question()
            store = session.bot.store
            task, _ = store.create(OWNER, "legacy-followup", "group", "g", {
                "text": followup, "action": "追问", "parent_id": original,
            })
            tid = task["task_id"]
            store.update(tid, state="completed", problem=PROBLEM, result={"solution": solution()})
            assert "把负根代回去试试。" in await session.send(f"/查错 {tid} 代入方程检查")
            assert followup in prompts[-1]
            assert "已收藏" in await session.send(f"/收藏 {tid}")
            assert followup in store.get_favorite(tid, OWNER)["problem"]
        finally:
            session.bot.store.close()

    asyncio.run(check())


def test_favoriting_a_question_while_solving_keeps_the_eventual_answer(tmp_path, monkeypatch):
    async def chat(messages, *args):
        if "规划器" in messages[0]["content"]:
            return '{"accepted": true, "domain": "math", "needs_search": false, "search_query": ""}'
        return json.dumps(solution(), ensure_ascii=False)

    async def render(*args):
        return {}

    monkeypatch.setattr("solver.render_artifacts_async", render)

    async def check():
        session = StudyChat(tmp_path, chat)
        try:
            store = session.bot.store
            task, _ = store.create(OWNER, "original", "group", "g", {"text": PROBLEM})
            tid = task["task_id"]
            store.update(tid, state="solving")
            assert "已收藏" in await session.send(f"/收藏 {tid}")
            await session.bot.solver.run(store.get(tid, OWNER))
            store.cleanup(ttl_days=-1)
            assert ANSWER in store.get_favorite(tid, OWNER)["answer"] and store.get(tid)
        finally:
            session.bot.store.close()

    asyncio.run(check())


@pytest.mark.parametrize("problem,draft,initial,corrected,calculation,result", [
    (PROBLEM, "草稿中写了 x=3。", "x=3 是根。", "候选根 x=3 代回原式不成立。",
     {"tool": "simplify", "args": {"expr": "3**2-4"}}, "5"),
    ("求 x² 的原函数。", "草稿中取 F=x³。", "F=x³ 的导数是 x²。", "原函数的系数需要调整。",
     {"tool": "diff", "args": {"expr": "x**3", "symbol": "x"}}, "3*x**2"),
])
def test_attempt_check_reconciles_actual_calculations_with_the_transcribed_draft(
        tmp_path, monkeypatch, problem, draft, initial, corrected, calculation, result):
    prompts = []

    async def chat(messages, *args):
        prompts.append(messages)
        if isinstance(messages[-1]["content"], list):
            return json.dumps({"problem_latex": draft, "notes": ""}, ensure_ascii=False)
        if len(prompts) == 2:
            return json.dumps({"status": "complete", "feedback": initial, "checks": [calculation]}, ensure_ascii=False)
        return json.dumps({"status": "complete", "feedback": corrected, "used_checks": [1]}, ensure_ascii=False)

    async def check():
        session = StudyChat(tmp_path, chat, sympy=True)
        try:
            original = session.solved_question(problem=problem)
            reply = await session.send(f"/查错 {original} 我怀疑这一步", attachments=photo(monkeypatch))
            assert corrected in reply and initial not in reply
            assert calculation["args"]["expr"] in reply and f"= {result}" in reply
            assert len(prompts) == 3 and "逐行" in prompts[0][0]["content"]
            assert problem in prompts[1][-1]["content"] and draft in prompts[1][-1]["content"]
            reconciliation = json.loads(prompts[2][-1]["content"])
            assert reconciliation["problem"] == problem and draft in reconciliation["draft"]
            assert reconciliation["initial_feedback"] == initial
            assert reconciliation["calculations"] == [{"call_id": 1, **calculation, "ok": True, "result": result}]
            assert all(ANSWER not in prompt[-1]["content"] for prompt in prompts[1:])
            assert session.latest()["problem"] == problem
        finally:
            session.bot.store.close()

    asyncio.run(check())


@pytest.mark.parametrize("failure", ["timeout", "unresolved", "unknown_record"])
def test_attempt_check_reports_uncertainty_when_reconciliation_cannot_resolve_conflict(tmp_path, failure):
    initial = "x=3 是根。"
    prompts = []

    async def chat(messages, *args):
        prompts.append(messages)
        if len(prompts) == 1:
            return json.dumps({"status": "complete", "feedback": initial,
                               "checks": [{"tool": "simplify", "args": {"expr": "3**2-4"}}]})
        if failure == "timeout":
            raise AIError("模型响应超时", "timeout")
        return json.dumps({"status": "uncertain" if failure == "unresolved" else "complete",
                           "feedback": initial, "used_checks": [1 if failure == "unresolved" else 2]})

    async def check():
        session = StudyChat(tmp_path, chat, sympy=True)
        try:
            original = session.solved_question()
            reply = await session.send(f"/查错 {original} 草稿中写了 x=3。")
            assert initial not in reply
            assert "3**2-4 = 5" in reply
            assert session.latest()["result"]["answer_status"] == "uncertain"
            assert len(prompts) == 2
        finally:
            session.bot.store.close()

    asyncio.run(check())


def test_attempt_check_does_not_attach_calculations_for_a_different_expression(tmp_path):
    corrected = "原式是 x²=9，草稿代入 x=3 成立。"
    prompts = []

    async def chat(messages, *args):
        prompts.append(messages)
        if len(prompts) == 1:
            return json.dumps({"status": "complete", "feedback": "x=3 代入后不成立。",
                               "checks": [{"tool": "simplify", "args": {"expr": "3**2-4"}}]})
        return json.dumps({"status": "complete", "feedback": corrected, "used_checks": []})

    async def check():
        session = StudyChat(tmp_path, chat, sympy=True)
        try:
            original = session.solved_question(problem="检查 x=3 是否满足 x²=9。")
            reply = await session.send(f"/查错 {original} 草稿中把 x=3 代入等式。")
            assert corrected in reply and "3**2-4" not in reply
        finally:
            session.bot.store.close()

    asyncio.run(check())


@pytest.mark.parametrize("sympy,checks", [
    (False, [{"tool": "simplify", "args": {"expr": "3**2-4"}}]),
    (True, []),
    (True, [{"tool": "simplify", "args": {"expr": "3^2-4"}}]),
])
def test_attempt_check_skips_reconciliation_without_a_successful_calculation(tmp_path, sympy, checks):
    feedback = "请把草稿中的 x=3 代回等式两侧。"
    prompts = []

    async def chat(messages, *args):
        prompts.append(messages)
        return json.dumps({"status": "uncertain", "feedback": feedback, "checks": checks})

    async def check():
        session = StudyChat(tmp_path, chat, sympy=sympy)
        try:
            original = session.solved_question()
            reply = await session.send(f"/查错 {original} 草稿中写了 x=3。")
            assert feedback in reply and "符号计算" not in reply
            assert len(prompts) == 1
        finally:
            session.bot.store.close()

    asyncio.run(check())


def test_study_commands_require_an_owned_question_and_a_nonempty_attempt(tmp_path):
    async def chat(*args):
        pytest.fail("Missing or inaccessible material should not call the model")

    async def check():
        session = StudyChat(tmp_path, chat)
        try:
            original = session.solved_question()
            assert "草稿" in await session.send(f"/查错 {original}")
            assert "未找到" in await session.send(f"/查错 {original} 尝试", owner="group:g:someone-else")
            assert "未找到" in await session.send(f"/查错 {original} 自己的草稿", owner="group:other:learner")
            assert len(session.bot.store.list_recent(OWNER)) == 1
        finally:
            session.bot.store.close()

    asyncio.run(check())
