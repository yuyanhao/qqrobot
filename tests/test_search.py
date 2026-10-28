import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from search_tools import DEFAULT_ENDPOINT, SearchError, deepseek_search, parse_results
from solver import Solver
from task_store import TaskStore


def fake_session(monkeypatch, data, status=200, error=None):
    seen = []

    class Response:
        async def __aenter__(self):
            if error:
                raise error
            return self

        async def __aexit__(self, *args):
            pass

        async def json(self, **kwargs):
            if isinstance(data, Exception):
                raise data
            return data

    Response.status = status

    class Session:
        def __init__(self, **kwargs):
            seen.append({"session": kwargs})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def post(self, endpoint, **kwargs):
            seen.append({"endpoint": endpoint, **kwargs})
            return Response()

    monkeypatch.setattr("search_tools.aiohttp.ClientSession", Session)
    return seen


def page():
    return {"title": "官方定理", "url": "https://example.org/theorem", "snippet": "简述",
            "summary": "定理条件", "content": "完整正文", "site_name": "官方", "date": "2026-10-28T00:00:00Z"}


def test_native_search_uses_deepseek_key_official_schema_and_content(monkeypatch):
    seen = fake_session(monkeypatch, {"results": [page()]})
    result = asyncio.run(deepseek_search("synthetic-deepseek-key", "  中文定理  ", count=4))
    assert seen[1] == {"endpoint": DEFAULT_ENDPOINT,
                       "json": {"query": "中文定理", "need_content": True, "count": 4},
                       "headers": {"Authorization": "Bearer synthetic-deepseek-key", "Content-Type": "application/json"}}
    assert result[0]["content"] == "完整正文"
    assert result[0]["site"] == "官方"
    assert result[0]["date"] == page()["date"]


@pytest.mark.parametrize("count,expected", [(0, 1), (100, 50)])
def test_search_respects_unicode_query_and_result_limits(monkeypatch, count, expected):
    seen = fake_session(monkeypatch, {"results": []})
    assert asyncio.run(deepseek_search("fake", "数" * 200, count=count)) == []
    assert seen[1]["json"]["query"] == "数" * 128
    assert seen[1]["json"]["count"] == expected


@pytest.mark.parametrize("key,query,match", [("", "定理", "DEEPSEEK_API_KEY"), ("fake", " \n ", "搜索词为空")])
def test_invalid_search_input_never_sends_request(monkeypatch, key, query, match):
    seen = fake_session(monkeypatch, {"results": []})
    with pytest.raises(SearchError, match=match):
        asyncio.run(deepseek_search(key, query))
    assert not seen


@pytest.mark.parametrize("status,match", [(401, "Key 无效"), (402, "余额不足"), (403, "无权限"),
                                        (429, "太频繁"), (500, "HTTP 500")])
def test_api_errors_are_explicit_without_echoing_credentials(monkeypatch, status, match):
    fake_session(monkeypatch, {"error": "synthetic-secret"}, status=status)
    with pytest.raises(SearchError, match=match) as caught:
        asyncio.run(deepseek_search("synthetic-secret", "定理"))
    assert "synthetic-secret" not in str(caught.value)


@pytest.mark.parametrize("data,match", [(ValueError("broken json"), "不是 JSON"), ({}, "格式异常"),
                                      ([], "格式异常"), ({"results": None}, "格式异常")])
def test_invalid_response_is_not_reported_as_empty_success(monkeypatch, data, match):
    fake_session(monkeypatch, data)
    with pytest.raises(SearchError, match=match):
        asyncio.run(deepseek_search("fake", "定理"))


def test_network_failure_is_reported_without_echoing_exception_secrets(monkeypatch):
    fake_session(monkeypatch, {}, error=TimeoutError("synthetic-secret"))
    with pytest.raises(SearchError, match="TimeoutError") as caught:
        asyncio.run(deepseek_search("fake", "定理"))
    assert "synthetic-secret" not in str(caught.value)


def test_result_filtering_keeps_unique_public_urls_and_optional_fields():
    assert parse_results({"results": []}) == []
    results = parse_results({"results": [None, {}, {"url": "file:///secret"}, page(), page(),
                                          {"url": "https://example.org/other", "date": None}]})
    assert len(results) == 2
    assert results[1]["title"] == "未命名结果" and results[1]["date"] == ""


def test_search_evidence_includes_date_and_bounded_body_without_local_crawling(monkeypatch):
    async def search(key, query, **kwargs):
        assert key == "fake-deepseek" and kwargs["count"] == 4
        return [{**page(), "content": "正" * 6000}] * 4

    async def no_download(*args, **kwargs):
        raise AssertionError("native search must supply content")

    monkeypatch.setattr("solver.deepseek_search", search)
    monkeypatch.setattr("solver.download_file", no_download)
    config = SimpleNamespace(search=True, search_key="fake-deepseek", search_endpoint=DEFAULT_ENDPOINT)
    evidence, sources = asyncio.run(Solver(None, None, config).search("定理"))
    assert "检索日期（北京时间）" in evidence and page()["date"] in evidence
    assert evidence.count("页面正文：") == 2 and "正" * 5001 not in evidence
    assert len(sources) == 4 and sources[0]["date"] == page()["date"]


@pytest.mark.parametrize("enabled,key", [(False, "fake"), (True, "")])
def test_unavailable_search_does_not_claim_online_access(enabled, key):
    evidence, sources = asyncio.run(Solver(None, None, SimpleNamespace(search=enabled, search_key=key)).search("定理"))
    assert "不要声称已联网" in evidence and sources == []


@pytest.mark.parametrize("planner_search,failed", [(True, False), (False, False), (True, True), (False, True)])
def test_both_search_paths_feed_evidence_to_solving_and_verification(monkeypatch, tmp_path, planner_search, failed):
    evidence_seen, queries = [], []

    class AI:
        async def plan_problem(self, *args):
            return {"accepted": True, "needs_search": planner_search, "search_query": "定理"}

        async def solve(self, problem, extra, timeout, calculations, evidence):
            evidence_seen.append(evidence)
            return {"title": "测试", "problem_latex": problem, "steps": [{"text": "展开", "math": "x=1"}],
                    "answer_latex": "1", "check_latex": "", "status": "complete", "search_query": "补查"}

        async def verify(self, *args, evidence, **kwargs):
            evidence_seen.append(evidence)
            return {"ok": True}

    async def search(key, query, **kwargs):
        queries.append(query)
        if failed:
            raise SearchError("模拟搜索失败")
        return [page()]

    async def render(*args):
        return {"total_pages": 1, "preview_paths": []}

    monkeypatch.setattr("solver.deepseek_search", search)
    monkeypatch.setattr("solver.render_artifacts_async", render)
    config = SimpleNamespace(search=True, search_key="fake", search_endpoint=DEFAULT_ENDPOINT,
                             sympy=False, verify=True, solve_timeout=600, verify_timeout=300)
    store = TaskStore(tmp_path)
    try:
        task, _ = store.create("u", "m", "c2c", "u", {"text": "解释定理", "attachments": []})
        asyncio.run(Solver(store, AI(), config).run(task))
        result = store.get(task["task_id"])["result"]
        assert len(queries) == 1
        assert queries[0] == ("定理" if planner_search else "补查")
        assert evidence_seen[-1] == evidence_seen[-2]
        if failed:
            assert "不能声称已获取来源" in evidence_seen[-1]
            assert result["sources"] == [] and "搜索未完成" in result["solution"]["check_latex"]
        else:
            assert "完整正文" in evidence_seen[-1] and result["sources"][0]["url"] == page()["url"]
            assert page()["url"] in Path(result["text_path"]).read_text(encoding="utf-8")
    finally:
        store.close()
