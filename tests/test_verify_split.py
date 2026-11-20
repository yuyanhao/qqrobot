"""Verification is a cheap verdict first, then an optional rewrite on demand."""
import asyncio

import pytest

from ai_fixtures import model_client
from qqrobot.solving.ai import AIError, validate_judgement

PROBLEM = "求 $\\int x^2 \\, dx$"
SOLUTION = {"title": "解答", "problem_latex": PROBLEM, "steps": [{"text": "按幂函数积分", "math": "x^3/3+C"}],
            "answer_latex": "x^3/3+C", "check_latex": "", "status": "complete",
            "search_query": "", "plots": []}


def make_ai():
    return model_client()


def test_a_passing_verdict_never_asks_for_a_rewrite():
    async def check():
        ai = make_ai()
        calls = []

        async def judge(problem, solution, timeout, *, extra="", evidence=""):
            calls.append(("judge", timeout))
            return {"ok": True, "errors": [], "rewrite": False, "fix_plan": ""}

        ai.verify_judge = judge
        try:
            checked = await ai.verify(PROBLEM, SOLUTION, timeout=600)
            assert checked == {"ok": True, "errors": [], "corrected": None}
            # The judge keeps everything but the slice held back for a rewrite.
            assert calls == [("judge", 420)]
        finally:
            await ai.close()
    asyncio.run(check())


def test_a_failed_verdict_rewrites_once_with_the_remaining_budget():
    async def check():
        ai = make_ai()
        calls = []

        async def judge(problem, solution, timeout, *, extra="", evidence=""):
            return {"ok": False, "errors": ["没有说明积分常数"], "rewrite": True, "fix_plan": "补上常数说明"}

        async def rewrite(problem, solution, judgement, timeout, *, extra="", evidence=""):
            calls.append(("rewrite", timeout, judgement["fix_plan"]))
            return dict(SOLUTION, steps=[{"text": "补上常数说明", "math": ""}])

        ai.verify_judge = judge
        ai.verify_rewrite = rewrite
        try:
            checked = await ai.verify(PROBLEM, SOLUTION, timeout=600)
            assert checked["ok"] is False and checked["errors"] == ["没有说明积分常数"]
            assert checked["corrected"]["steps"][0]["text"] == "补上常数说明"
            assert "rewrite_skipped" not in checked
            assert calls[0][0] == "rewrite" and calls[0][2] == "补上常数说明"
            assert calls[0][1] > 90
        finally:
            await ai.close()
    asyncio.run(check())


def test_a_judge_without_a_rewrite_plan_stops_after_the_verdict():
    async def check():
        ai = make_ai()
        called = []

        async def judge(problem, solution, timeout, *, extra="", evidence=""):
            return {"ok": False, "errors": ["条件不足，无法确认"], "rewrite": False, "fix_plan": ""}

        async def rewrite(*args, **kwargs):
            called.append(True)
            return SOLUTION

        ai.verify_judge = judge
        ai.verify_rewrite = rewrite
        try:
            checked = await ai.verify(PROBLEM, SOLUTION, timeout=600)
            assert checked["ok"] is False and checked["corrected"] is None
            assert not called
        finally:
            await ai.close()
    asyncio.run(check())


def test_rewrite_is_skipped_when_the_judge_used_up_the_budget():
    async def check():
        ai = make_ai()
        called = []

        async def judge(problem, solution, timeout, *, extra="", evidence=""):
            await asyncio.sleep(0.2)
            return {"ok": False, "errors": ["第三步跳步"], "rewrite": True, "fix_plan": "补证明"}

        async def rewrite(*args, **kwargs):
            called.append(True)
            return SOLUTION

        ai.verify_judge = judge
        ai.verify_rewrite = rewrite
        try:
            checked = await ai.verify(PROBLEM, SOLUTION, timeout=30)
            assert checked["ok"] is False and checked["corrected"] is None
            assert checked["rewrite_skipped"] is True
            assert not called
        finally:
            await ai.close()
    asyncio.run(check())


def test_a_failed_rewrite_keeps_the_verdict():
    async def check():
        ai = make_ai()

        async def judge(problem, solution, timeout, *, extra="", evidence=""):
            return {"ok": False, "errors": ["结论与条件不符"], "rewrite": True, "fix_plan": "重推第二步"}

        async def rewrite(*args, **kwargs):
            raise AIError("复核修订超时", "timeout", "复核修订")

        ai.verify_judge = judge
        ai.verify_rewrite = rewrite
        try:
            checked = await ai.verify(PROBLEM, SOLUTION, timeout=600)
            assert checked["ok"] is False and checked["errors"] == ["结论与条件不符"]
            assert checked["corrected"] is None and checked["rewrite_skipped"] is True
        finally:
            await ai.close()
    asyncio.run(check())


def test_judgement_validation_rejects_malformed_verdicts():
    with pytest.raises(AIError):
        validate_judgement({"errors": []})
    with pytest.raises(AIError):
        validate_judgement({"ok": "true"})
    with pytest.raises(AIError):
        validate_judgement({"ok": True, "errors": "没问题"})
    assert validate_judgement({"ok": True, "errors": [], "rewrite": True})["rewrite"] is False
    assert validate_judgement({"ok": False, "errors": ["x"], "rewrite": True, "fix_plan": "改"})["rewrite"] is True
