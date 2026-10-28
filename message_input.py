from __future__ import annotations
import re
from urllib.parse import urlparse
MENTION_RE = re.compile(r"<@!?\d+>")
_TEXT_EXTS = (".md", ".markdown", ".txt", ".tex")


def clean_text(raw: str) -> str:
    return MENTION_RE.sub(" ", raw or "").strip()

def message_type(raw: dict) -> int:
    try:
        return int(raw.get("message_type") or 0)
    except (TypeError, ValueError):
        return 0

def _walk_quoted(
    elements,
    texts: list[str],
    attachments: list[dict],
    depth: int = 0,
) -> None:
    if depth > 5:
        return
    for element in elements or []:
        if not isinstance(element, dict):
            continue
        content = str(element.get("content") or "").strip()
        if content:
            texts.append(content)
        for attachment in element.get("attachments") or []:
            if isinstance(attachment, dict):
                attachments.append(attachment)
        _walk_quoted(
            element.get("msg_elements"),
            texts,
            attachments,
            depth + 1,
        )

def extract_quoted(raw: dict) -> tuple[str, list[dict]]:
    """QQ 引用消息（message_type=103）的文字和附件在 msg_elements 里。

    引用里再套引用时，附件可能在更深层的 msg_elements，所以递归收集。
    """
    if message_type(raw) != 103:
        return "", []
    texts: list[str] = []
    attachments: list[dict] = []
    _walk_quoted(raw.get("msg_elements"), texts, attachments)
    return "\n".join(texts)[:4000], attachments

def _attachment_field(attachment, names: tuple[str, ...]) -> str:
    if isinstance(attachment, dict):
        for name in names:
            value = attachment.get(name)
            if value:
                return str(value)
    else:
        for name in names:
            value = getattr(attachment, name, None)
            if value:
                return str(value)
    return ""

def _is_pdf(attachment: dict) -> bool:
    content_type = str(attachment.get("content_type") or "").lower()
    if content_type.startswith("application/pdf"):
        return True
    name = str(attachment.get("name") or "").lower()
    if name.endswith(".pdf"):
        return True
    url = str(attachment.get("url") or "").lower()
    return urlparse(url).path.endswith(".pdf")

def _is_text_file(attachment: dict) -> bool:
    content_type = str(attachment.get("content_type") or "").lower()
    if content_type.startswith("text/"):
        return True
    name = str(attachment.get("name") or "").lower()
    if name.endswith(_TEXT_EXTS):
        return True
    url = str(attachment.get("url") or "").lower()
    return urlparse(url).path.endswith(_TEXT_EXTS)

def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")

def collect_attachments(message, raw: dict) -> tuple[str, list[dict]]:
    """当前消息附件 + 引用消息附件，按 URL 去重。"""
    quoted_text, quoted_attachments = extract_quoted(raw)
    items: list[dict] = []
    seen: set[str] = set()

    for attachment in getattr(message, "attachments", None) or []:
        url = _attachment_field(
            attachment, ("url", "file_url", "fileUrl", "download_url", "downloadUrl")
        )
        content_type = _attachment_field(
            attachment, ("content_type", "contentType", "mime_type")
        )
        name = _attachment_field(attachment, ("filename", "file_name", "name"))
        if url and url not in seen:
            items.append(
                {"url": url, "content_type": content_type, "name": name}
            )
            seen.add(url)

    for attachment in quoted_attachments:
        url = _attachment_field(
            attachment, ("url", "file_url", "fileUrl", "download_url", "downloadUrl")
        )
        content_type = _attachment_field(
            attachment, ("content_type", "contentType", "mime_type")
        )
        name = _attachment_field(attachment, ("filename", "file_name", "name"))
        if url and url not in seen:
            items.append(
                {"url": url, "content_type": content_type, "name": name}
            )
            seen.add(url)

    return quoted_text, items
