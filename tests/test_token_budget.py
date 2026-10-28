"""Exercise solve/verify through the real API wrapper with long-thinking streams."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from ai import AIError, DeepSeek


def document():
    return {"title": "解答", "problem_latex": "求积分", "steps": [{"text": "按定义计算", "math": "1/3"}],
            "answer_latex": "1/3", "check_latex": "", "status": "complete", "plots": []}


class Stream:
    def __init__(self, content, finish):
        self.closed = False
        self.items = iter([
            SimpleNamespace(usage=None, choices=[SimpleNamespace(
                delta=SimpleNamespace(content=None, reasoning_content="private-reasoning-fixture"), finish_reason=None)]),
            SimpleNamespace(usage=SimpleNamespace(prompt_tokens=100, completion_tokens=40000,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=39000)), choices=[SimpleNamespace(
                delta=SimpleNamespace(content=content, reasoning_content=None), finish_reason=finish)])])
    def __aiter__(self): return self
    async def __anext__(self):
        try: return next(self.items)
        except StopIteration: raise StopAsyncIteration
    async def close(self): self.closed = True


@pytest.mark.parametrize("effort,expected", [("high", 65536), ("max", 131072)])
def test_long_reasoning_budget_reaches_actual_solve_and_verify(monkeypatch, effort, expected):
    async def check():
        monkeypatch.delenv("SOLVE_MAX_TOKENS", raising=False)
        monkeypatch.delenv("VERIFY_MAX_TOKENS", raising=False)
        ai = DeepSeek("fake-key", "https://api.deepseek.com", "deepseek-flash", effort)
        calls, streams = [], []
        async def create(**kwargs):
            calls.append(kwargs)
            complete = kwargs["max_tokens"] >= 40000
            payload = document() if len(calls) == 1 else {"ok": True, "errors": [], "corrected": None}
            stream = Stream(json.dumps(payload) if complete else "", "stop" if complete else "length")
            streams.append(stream)
            return stream
        ai.client.chat.completions.create = create
        try:
            result = await ai.solve("求积分")
            checked = await ai.verify("求积分", result)
            assert result["status"] == "complete" and checked["ok"]
            assert [c["max_tokens"] for c in calls] == [expected, expected]
            assert all(c["reasoning_effort"] == effort for c in calls)
            assert all(s.closed for s in streams)
        finally:
            await ai.close()
    asyncio.run(check())


def test_token_budget_overrides_and_limits(monkeypatch):
    monkeypatch.setenv("SOLVE_MAX_TOKENS", "96000")
    monkeypatch.setenv("VERIFY_MAX_TOKENS", "9999999")
    ai = DeepSeek("fake-key", "https://api.deepseek.com", "deepseek-flash", "max")
    try:
        assert ai.solve_max_tokens == 96000
        assert ai.verify_max_tokens == 131072
    finally:
        asyncio.run(ai.close())


def test_length_never_passes_as_complete_or_logs_reasoning(caplog):
    async def check():
        stream = Stream(json.dumps(document()), "length")
        with pytest.raises(AIError) as caught:
            await DeepSeek._collect_stream(stream)
        assert caught.value.code == "truncated" and stream.closed
        assert "private-reasoning-fixture" not in caplog.text
    asyncio.run(check())
