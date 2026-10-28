"""DeepSeek 官方 Search API，复用模型 API Key。"""

from __future__ import annotations

import logging

import aiohttp

log = logging.getLogger("qq-deepseek-bot.search")

DEFAULT_ENDPOINT = "https://api.deepseek.com/v1/tools/search"


class SearchError(RuntimeError):
    """搜索 API 调用失败。"""


def parse_results(data: dict) -> list[dict]:
    items = data.get("results") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise SearchError("DeepSeek 搜索返回格式异常：缺少 results 列表")

    results: list[dict] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        results.append(
            {
                "title": str(item.get("title") or "").strip()
                or "未命名结果",
                "url": url,
                "snippet": str(item.get("snippet") or "").strip(),
                "summary": str(item.get("summary") or "").strip(),
                "content": str(item.get("content") or "").strip(),
                "site": str(item.get("site_name") or "").strip(),
                "date": str(item.get("date") or "").strip(),
            }
        )
    return results


async def deepseek_search(
    api_key: str,
    query: str,
    count: int = 5,
    timeout: int = 20,
    endpoint: str = DEFAULT_ENDPOINT,
) -> list[dict]:
    if not api_key:
        raise SearchError("还没有配置 DEEPSEEK_API_KEY")
    query = (query or "").strip()[:128]
    if not query:
        raise SearchError("搜索词为空")

    payload = {
        "query": query,
        "need_content": True,
        "count": max(1, min(int(count), 50)),
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    timeout_obj = aiohttp.ClientTimeout(total=max(5, int(timeout)))

    try:
        async with aiohttp.ClientSession(timeout=timeout_obj) as session:
            async with session.post(endpoint, json=payload, headers=headers) as resp:
                if resp.status >= 400:
                    if resp.status == 401:
                        raise SearchError("DeepSeek API Key 无效（401）")
                    if resp.status == 402:
                        raise SearchError("DeepSeek 余额不足（402）")
                    if resp.status == 403:
                        raise SearchError("DeepSeek 搜索无权限（403）")
                    if resp.status == 429:
                        raise SearchError("DeepSeek 搜索请求太频繁（429），稍后再试")
                    raise SearchError(f"DeepSeek 搜索失败 HTTP {resp.status}")
                try:
                    data = await resp.json(content_type=None)
                except Exception as exc:
                    raise SearchError("DeepSeek 搜索返回的不是 JSON") from exc
    except SearchError:
        raise
    except Exception as exc:
        raise SearchError(f"DeepSeek 搜索连接失败（{type(exc).__name__}）") from exc

    results = parse_results(data)
    log.info("搜索完成，结果数=%d", len(results))
    return results
