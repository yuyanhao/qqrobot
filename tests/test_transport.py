import asyncio
import base64
import hashlib
from pathlib import Path
import time

from qqmedia import QQMedia
from task_bot import TaskBot, ReplyContext
from task_store import TaskStore
from types import SimpleNamespace


def test_small_pdf_upload_uses_file_type_and_new_query_id(tmp_path):
    async def check():
        media = QQMedia("fake-id", "fake-secret")
        calls = []
        async def request(path, payload):
            calls.append((path, payload))
            return {"file_info": "fake-info"}
        media._request = request
        pdf = tmp_path / "answer.pdf"
        pdf.write_bytes(b"%PDF-1.4 synthetic")
        info = await media.upload_file_path("c2c", "user", pdf)
        await media.send_file("c2c", "user", info, "fresh-query", 2)
        assert calls[0][1]["file_type"] == 4 and calls[0][1]["srv_send_msg"] is False
        assert base64.b64decode(calls[0][1]["file_data"]) == pdf.read_bytes()
        assert calls[1][1]["msg_id"] == "fresh-query" and calls[1][1]["msg_seq"] == 2
        assert "upload_id" not in calls[0][1]
    asyncio.run(check())


def test_large_local_pdf_chunks_without_public_hosting(tmp_path, monkeypatch):
    async def check():
        media = QQMedia("fake-id", "fake-secret")
        calls, blocks = [], []
        class Response:
            status = 200
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
        class Session:
            def put(self, url, *, data, allow_redirects):
                assert not allow_redirects
                blocks.append(data)
                return Response()
        async def session(): return Session()
        async def public(url): pass
        monkeypatch.setattr("qqmedia._check_public_url", public)
        media._get_session = session
        async def request(path, payload):
            calls.append((path, payload))
            if path.endswith("upload_prepare"):
                return {"upload_id": "test-upload", "block_size": "3000000", "parts": [
                    {"index": 0, "presigned_url": "https://example.com/0"},
                    {"index": 1, "presigned_url": "https://example.com/1"}]}
            return {"file_info": "done"} if path.endswith("files") else {}
        media._request = request
        data = b"test-data-" * 500001
        pdf = tmp_path / "large.pdf"
        pdf.write_bytes(data)
        assert await media.upload_file_path("group", "group", pdf) == "done"
        assert b"".join(blocks) == data
        assert calls[0][1]["md5_10m"] == hashlib.md5(data, usedforsecurity=False).hexdigest()
        assert [call[1]["part_index"] for call in calls if call[0].endswith("upload_part_finish")] == [0, 1]
        assert calls[-1][1]["upload_id"] == "test-upload"
        assert all("url" not in payload for _, payload in calls)
    asyncio.run(check())


def test_delivery_failure_keeps_result_for_later_query(tmp_path):
    async def check():
        store = TaskStore(tmp_path)
        task, _ = store.create("u", "old-message", "c2c", "u", {"text": "题目"})
        path = store.task_dir(task["task_id"]) / "answer.pdf"
        path.write_bytes(b"%PDF-1.4 complete synthetic answer")
        store.update(task["task_id"], state="completed", result={"pdf_path": str(path), "total_pages": 4})
        async def upload(*args): raise RuntimeError("synthetic QQ failure")
        bot = object.__new__(TaskBot)
        bot.store = store
        bot.settings = SimpleNamespace(active_delivery=False)
        bot.media = SimpleNamespace(upload_file_path=upload)
        sent = []
        async def text(context, value): sent.append(value); return True
        bot._text = text
        await bot._deliver(store.get(task["task_id"]), ReplyContext("c2c", "u", "u", "new-message", time.time()))
        row = store.get(task["task_id"])
        assert row["state"] == "completed" and row["delivery"] == "awaiting_query"
        assert path.read_bytes() == b"%PDF-1.4 complete synthetic answer"
        assert "结果已保存" in sent[-1]
        store.close()
    asyncio.run(check())
