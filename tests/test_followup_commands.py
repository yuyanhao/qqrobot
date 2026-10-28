import asyncio
from collections import OrderedDict
from types import SimpleNamespace

import pytest

from solver import Solver
from task_bot import TaskBot
from task_store import TaskStore


@pytest.mark.parametrize("prefix", ["/", "／", "/ ", "／ ", ""])
def test_followup_command_reuses_problem_and_solution(tmp_path, prefix):
    async def check():
        store = TaskStore(tmp_path)
        problem = "小球与地面碰撞，取向上为正，求速度随时间的变化。"
        parent, _ = store.create("group:g:u", "original", "group", "g", {"text": problem})
        store.update(parent["task_id"], state="completed", problem=problem,
                     result={"solution": {"answer_latex": "原题的分段速度表达式"}})
        bot = object.__new__(TaskBot)
        bot.store, bot.seen = store, OrderedDict()
        bot._ensure_worker = lambda: None
        submitted, replies = [], []
        async def submit(context, request):
            submitted.append(request)
        async def reply(context, text):
            replies.append(text)
        bot._submit, bot._text = submit, reply
        message = SimpleNamespace(id="followup", content=f"{prefix}追问 {parent['task_id']} 你能不能画出来v-t图像")
        try:
            await bot._handle(message, "group", "g", "group:g:u")
            assert len(submitted) == 1, replies
            assert submitted[0].get("parent_id") == parent["task_id"]
            child, _ = store.create("group:g:u", "followup", "group", "g", submitted[0])
            original, extra, _ = await Solver(store, None, SimpleNamespace()).inputs(child, store.task_dir(child["task_id"]))
            assert original == problem
            assert "你能不能画出来v-t图像" in extra
            assert "原题的分段速度表达式" in extra
        finally:
            store.close()
    asyncio.run(check())
