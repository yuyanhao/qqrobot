"""Keep collected attachments locally until the owner starts the problem."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
from uuid import uuid4

from file_tools import DownloadError, download_file
from message_input import _is_pdf, _is_text_file


class InputCacheError(ValueError):
    pass


def attachment_limit(item):
    return 20 if _is_pdf(item) else 2 if _is_text_file(item) else 8


def cached_path(store, task, item):
    sid, name = item.get("source_task_id", ""), item.get("cached_name", "")
    if not isinstance(name, str) or not re.fullmatch(r"[0-9a-f]{32}\.bin", name):
        raise InputCacheError("附件缓存名称无效，请重新补图")
    if not isinstance(sid, str) or not re.fullmatch(r"Q\d{6}-\d{6,10}", sid, re.I):
        raise InputCacheError("附件来源题号无效，请重新补图")
    source = store.get(sid, task["owner"])
    if not source or source["state"] == "expired":
        raise InputCacheError("附件来源已过期或不属于本会话，请重新补图")
    root = store.task_dir(source["task_id"])
    directory = root / "inputs"
    path = directory / name
    if directory.is_symlink() or path.is_symlink() or path.resolve().parent != directory.resolve():
        raise InputCacheError("附件缓存路径无效，请重新补图")
    return path


def read_cached_attachment(store, task, item):
    if not item.get("cached_name"):
        return None
    path = cached_path(store, task, item)
    if not path.is_file() or not 0 < path.stat().st_size <= attachment_limit(item) * 1024**2:
        raise InputCacheError("附件缓存缺失或大小无效，请重新发送原图")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != item.get("sha256"):
        raise InputCacheError("附件缓存校验失败，请重新发送原图")
    return data


def discard_cached_attachments(store, task, items):
    for item in items:
        if item.get("source_task_id") == task["task_id"]:
            cached_path(store, task, item).unlink(missing_ok=True)


async def cache_attachments(store, task, attachments, *, max_total_mb=512):
    if len(attachments) > 4:
        raise InputCacheError("每条消息最多 4 个附件，可分多条用 /补图 追加")
    previous = task["request"].get("attachments", [])
    urls = {item.get("url") for item in previous}
    hashes = {item.get("sha256") for item in previous}
    added = []
    try:
        for original in attachments:
            url = original.get("url", "")
            if url.startswith("//"):
                url = "https:" + url
            elif url and "://" not in url:
                url = "https://" + url
            if not url:
                raise InputCacheError("图片没有可用下载地址，请重新发送原图")
            if url in urls:
                continue
            urls.add(url)
            file = await download_file(url, max_mb=attachment_limit(original), timeout=45)
            digest = hashlib.sha256(file.data).hexdigest()
            if digest in hashes:
                continue
            if len(previous) + len(added) >= 12:
                raise InputCacheError("同一道收集题最多 12 个附件，请分题提交")
            if not file.data or len(file.data) > attachment_limit(original) * 1024**2:
                raise InputCacheError("附件为空或超过大小限制，请重新发送")
            if store.disk_bytes() + len(file.data) > max_total_mb * 1024**2:
                raise InputCacheError("材料缓存已达到容量预算，请稍后再提交")
            item = {**original, "url": url, "cached_name": uuid4().hex + ".bin",
                    "source_task_id": task["task_id"], "sha256": digest}
            directory = store.task_dir(task["task_id"]) / "inputs"
            if directory.is_symlink():
                raise InputCacheError("附件缓存目录无效")
            directory.mkdir(exist_ok=True)
            added.append(item)
            cached_path(store, task, item).write_bytes(file.data)
            hashes.add(digest)
        return added
    except BaseException as exc:
        discard_cached_attachments(store, task, added)
        if isinstance(exc, DownloadError):
            raise InputCacheError(f"附件下载失败：{exc}。请重新发送原图。") from exc
        raise
