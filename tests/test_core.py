import asyncio
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai import AIError, DeepSeek, extract_json, normalize_solution, validate_check
from safe_math import Parser, run
from task_store import TaskStore, QueueFull
from task_bot import ReplyContext
from plot_tools import expression
from render import RenderError, sanitize_math
from pdf_tools import safe_page_scale
from process_tools import worker_environment


def doc():
    return {"title": "矩阵", "problem_latex": "求矩阵", "steps": [{"text": "按行展开", "math": r"\begin{pmatrix}a&b\\c&d\end{pmatrix}"}],
            "answer_latex": "", "check_latex": "", "status": "complete", "plots": []}


def test_matrix_round_trip_preserves_row_breaks():
    original = doc()
    assert normalize_solution(extract_json(json.dumps(original)))["steps"] == original["steps"]
    assert sanitize_math(original["steps"][0]["math"]) == original["steps"][0]["math"]


@pytest.mark.parametrize("value", [{}, {"steps": []}, {"steps": "42"}, {"steps": [{}]}])
def test_bad_solution_is_actionable(value):
    with pytest.raises(AIError) as caught: normalize_solution(value)
    assert caught.value.code == "schema"


def test_missing_verification_is_not_a_pass():
    with pytest.raises(AIError): validate_check({"errors": []})
    with pytest.raises(AIError): validate_check({"ok": "true"})


def test_json_repair_is_bounded_and_does_not_resolve():
    async def check():
        client = object.__new__(DeepSeek)
        seen = []
        async def fake(messages, *args):
            seen.append(messages)
            return json.dumps({"answer": "42"}) if len(seen) == 1 else json.dumps(doc())
        client._chat = fake
        result = await client._json([{"role": "system", "content": "JSON"}, {"role": "user", "content": "题目"}],
                                    stage="解题", validator=normalize_solution)
        assert result["status"] == "complete" and len(seen) == 2
        assert "不要重新解题" in seen[1][1]["content"]
        async def always_bad(*args): return "{}"
        client._chat = always_bad
        with pytest.raises(AIError) as caught:
            await client._json([{"role": "system", "content": "JSON"}], stage="解题", validator=normalize_solution)
        assert caught.value.stage == "解题"
    asyncio.run(check())


def test_stream_closes_on_empty_and_truncation():
    class Stream:
        closed = False
        def __aiter__(self): return self
        async def __anext__(self): raise StopAsyncIteration
        async def close(self): self.closed = True
    async def check():
        stream = Stream()
        with pytest.raises(AIError) as caught: await DeepSeek._collect_stream(stream)
        assert caught.value.code == "empty" and stream.closed
    asyncio.run(check())


def test_two_sided_limit_and_physical_symbols():
    both = run({"tool": "limit", "args": {"expr": "1/x", "symbol": "x", "point": "0"}})
    assert "left" in both and "right" in both and "-oo" in both
    assert run({"tool": "limit", "args": {"expr": "1/x", "symbol": "x", "point": "0", "direction": "right"}}) == "oo"
    assert run({"tool": "diff", "args": {"expr": "E**2", "symbol": "E"}}) == "2*E"
    assert run({"tool": "integrate", "args": {"expr": "x**2", "symbol": "x", "lower": "0", "upper": "1"}}) == "1/3"


@pytest.mark.parametrize("payload", ["__import__('os')", "x.__class__", "open('a')", "[i for i in range(3)]", "(lambda:1)()", "2**100000"])
def test_symbolic_parser_rejects_code(payload):
    with pytest.raises((ValueError, SyntaxError)): Parser({}).parse(payload)


@pytest.mark.parametrize("payload", [r"\csname input\endcsname{secret}", r"\input{secret}", r"\begin{document}", r"^^5cinput{secret}"])
def test_tex_read_bypass_is_rejected(payload):
    with pytest.raises(RenderError): sanitize_math(payload)


def test_large_pdf_scaled_before_allocation():
    scale = safe_page_scale(SimpleNamespace(get_size=lambda: (14400, 14400)), 160, 6_000_000)
    assert math.ceil(14400 * scale) ** 2 <= 6_000_000


def test_plots_interpret_numbers_and_reject_code():
    import numpy as np
    xs = np.array([0., 1., 2.])
    assert np.allclose(expression("x**2+1", "x", xs), [1, 2, 5])
    with pytest.raises(ValueError): expression("__import__('os').system('dir')", "x", xs)


def test_worker_does_not_inherit_api_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-secret")
    monkeypatch.setenv("QQ_SECRET", "synthetic-secret")
    env = worker_environment(tmp_path)
    assert "DEEPSEEK_API_KEY" not in env and "QQ_SECRET" not in env


def test_tasks_persist_are_scoped_and_deduplicated(tmp_path):
    store = TaskStore(tmp_path)
    one, created = store.create("alice", "m1", "c2c", "a", {"text": "求积分"})
    again, duplicate = store.create("alice", "m1", "c2c", "a", {"text": "求积分"})
    assert created and not duplicate and again["task_id"] == one["task_id"]
    assert store.get(one["task_id"], "bob") is None
    store.close()
    store = TaskStore(tmp_path)
    store.recover()
    assert store.get(one["task_id"])["state"] == "interrupted"
    store.close()


def test_queue_limit_and_new_task_not_eaten_by_old_result(tmp_path):
    store = TaskStore(tmp_path)
    one, _ = store.create("alice", "1", "c2c", "a", {"text": "第一题"}, max_active=1)
    with pytest.raises(QueueFull): store.create("bob", "2", "c2c", "b", {}, max_active=1)
    store.update(one["task_id"], state="completed", delivery="awaiting_query")
    two, created = store.create("alice", "3", "c2c", "a", {"text": "第二题"})
    assert created and two["task_id"] != one["task_id"]
    store.close()


def test_group_and_c2c_reply_windows(monkeypatch):
    monkeypatch.setattr("task_bot.time.time", lambda: 1000)
    assert not ReplyContext("group", "g", "u", "old", 600).passive_available()
    assert ReplyContext("c2c", "u", "u", "old", 600).passive_available()
    assert ReplyContext("group", "g", "u", "new-query", 1000).passive_available()
    assert not ReplyContext("c2c", "u", "u", "m", 1000, seq=4).passive_available()
