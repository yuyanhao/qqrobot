"""网页正文提取：给 /读 和“阅读这篇文章”用。"""

from __future__ import annotations

import logging
import re

import trafilatura

log = logging.getLogger("qq-deepseek-bot.reader")


def _decode(data: bytes) -> str:
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(data).best()
        if best is not None:
            return str(best)
    except Exception:
        pass
    for encoding in ("utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def extract_article(
    data: bytes,
    url: str = "",
    content_type: str = "",
    max_chars: int = 20000,
) -> str:
    text_type = (
        content_type.startswith("text/plain")
        or content_type.startswith("text/markdown")
        or url.lower().endswith((".md", ".txt"))
    )
    if text_type:
        return _decode(data).strip()[:max_chars]

    html = _decode(data)
    try:
        text = trafilatura.extract(
            html,
            url=url,
            include_comments=False,
            include_tables=True,
            favor_recall=True,
        )
    except Exception:
        log.exception("网页正文提取失败")
        text = None

    if not text:
        return ""
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:max_chars]
