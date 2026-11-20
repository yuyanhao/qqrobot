"""QQ SDK integration and process entry point for the task bot."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import datetime
import logging
import os
import re
import signal
import sqlite3
import time
from urllib.parse import urlparse

import botpy
from botpy.http import Route
from botpy.message import BaseMessage

# Load .env before importing modules that read optional limits at import time.
from qqrobot.settings import Settings, validate_environment

from qqrobot.providers import load_model_config
from qqrobot.solving.ai import ModelClient
from qqrobot.solving.book_library import BookLibrary
from qqrobot.core.message_input import _is_pdf, clean_text, collect_attachments
from qqrobot.media.qqmedia import QQMedia, qq_api_base_url
from qqrobot.solving.solver import Solver
from qqrobot.tasks.task_commands import HELP as HELP
from qqrobot.tasks.task_commands import TaskCommands
from qqrobot.tasks.task_delivery import ReplyContext, TaskDelivery
from qqrobot.tasks.task_queue import TaskQueue
from qqrobot.tasks.task_store import TaskStore

log = logging.getLogger("qqrobot.bot")
RAW_EVENTS = OrderedDict()
_original_message_init = BaseMessage.__init__


def _message_init(self, api, event_id, data):
    _original_message_init(self, api, event_id, data)
    if data.get("id"):
        RAW_EVENTS[data["id"]] = data
        while len(RAW_EVENTS) > 300: RAW_EVENTS.popitem(last=False)


BaseMessage.__init__ = _message_init


class TaskBot(TaskCommands, TaskQueue, TaskDelivery, botpy.Client):
    def __init__(self, intents, *, settings=None):
        warnings = validate_environment()
        self.settings = settings or Settings()
        api_base_url = qq_api_base_url()
        # qq-botpy 1.2.1 defaults to the old hostname. Keep SDK gateway
        # discovery and media requests on the same configured origin.
        Route.DOMAIN = urlparse(api_base_url).netloc
        # systemd/journald and Docker capture stderr; the application directory
        # is read-only, so the SDK must not create its default botpy.log there.
        super().__init__(intents=intents, ext_handlers=False)
        for warning in warnings:
            log.warning("configuration_warning %s", warning)
        self.store = TaskStore(self.settings.data_dir)
        self.store.recover()
        self.store.cleanup(self.settings.result_ttl)
        self._cleanup_retired_books()
        self.ai = ModelClient(load_model_config())
        self.media = QQMedia(os.environ["QQ_APPID"], os.environ["QQ_SECRET"], api_base_url,
                             active_delivery=self.settings.active_delivery)
        self.solver = Solver(self.store, self.ai, self.settings)
        self.queue = asyncio.Queue(maxsize=self.settings.max_queue)
        self.resource_lock = asyncio.Lock()
        self.collection_lock = asyncio.Lock()
        self.book_import_lock = asyncio.Lock()
        self.book_admin_openid = os.getenv("BOOK_ADMIN_OPENID", "").strip()
        self.pending_book_uploads = {}
        for pending_file in self.store.root.glob(".book-upload-*.pdf"):
            try:
                pending_file.unlink(missing_ok=True)
            except OSError:
                log.warning("pending_book_upload_cleanup_failed")
        BookLibrary(self.store.root).cleanup_staged()
        self.worker_task = None
        self.cleanup_task = None
        self.running_task = None
        self.running_id = None
        self.cancel_requested = set()
        self.contexts = {}
        self.seen = OrderedDict()
        self.allowed_groups = {x.strip() for x in os.getenv("ALLOWED_GROUPS", "").split(",") if x.strip()}
        self.allowed_users = {x.strip() for x in os.getenv("ALLOWED_USERS", "").split(",") if x.strip()}

    def _ensure_worker(self):
        if self.worker_task is None or self.worker_task.done():
            self.worker_task = asyncio.create_task(self._worker(), name="problem-worker")

    async def on_ready(self):
        self._ensure_worker()
        if self.cleanup_task is None or self.cleanup_task.done():
            self.cleanup_task = asyncio.create_task(self._cleanup_periodically(), name="expire-problems")
        log.info("problem_bot_ready")

    async def _cleanup_periodically(self):
        while True:
            await asyncio.sleep(3600)
            try:
                async with self.collection_lock:
                    self.store.cleanup(self.settings.result_ttl)
                    self._cleanup_retired_books()
            except (OSError, ValueError, sqlite3.Error):
                log.exception("expired_task_cleanup_failed")

    def _cleanup_retired_books(self):
        try:
            pending = self.store.pending_book_ids(self.settings.result_ttl)
            BookLibrary(self.store.root).purge_removed(pending)
        except (OSError, ValueError, sqlite3.Error):
            log.exception("retired_book_cleanup_failed")

    async def on_group_at_message_create(self, message):
        group, user = message.group_openid, message.author.member_openid
        if self.allowed_groups and group not in self.allowed_groups: return
        if not user: return
        await self._handle(message, "group", group, f"group:{group}:{user}")

    async def on_c2c_message_create(self, message):
        user = message.author.user_openid
        if not user or (self.allowed_users and user not in self.allowed_users): return
        await self._handle(message, "c2c", user, f"c2c:{user}")

    async def _handle(self, message, kind, target, owner):
        self._ensure_worker()
        event_key = (owner, message.id)
        if event_key in self.seen: return
        self.seen[event_key] = None
        while len(self.seen) > 2000: self.seen.popitem(last=False)
        raw = RAW_EVENTS.get(message.id, {})
        received = time.time()
        try:
            stamp = raw.get("timestamp")
            if stamp: received = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError): pass
        context = ReplyContext(kind, target, owner, message.id, received)
        try:
            text = clean_text(getattr(message, "content", "") or "").strip()
            text = re.sub(r"^[/／]\s*", "/", text)
            quoted, attachments, quoted_urls = collect_attachments(message, raw)
            if kind == "c2c" and not text.startswith("/"):
                pdfs = [item for item in attachments if _is_pdf(item)]
                unquoted = not any(item["url"] in quoted_urls for item in attachments)
                stage_attempted = False
                if target == self.book_admin_openid and attachments and unquoted:
                    stage_attempted = bool(pdfs) or (
                        len(attachments) == 1 and self._is_ambiguous_book_attachment(attachments[0]))
                    if stage_attempted and await self._stage_book_upload(context, attachments):
                        return
                if pdfs and not stage_attempted and not (text and any(item["url"] in quoted_urls for item in pdfs)):
                    return
            await self._dispatch_command(context, text, quoted, attachments, quoted_urls=quoted_urls)
        except Exception as exc:
            log.warning("message_handler_failed event=%s type=%s", message.id, type(exc).__name__, exc_info=True)
            await self._text(context, "内部错误：有题号请发题号查询，否则请重发。")

    async def shutdown(self):
        if self.cleanup_task:
            self.cleanup_task.cancel()
            try: await self.cleanup_task
            except asyncio.CancelledError: pass
        if self.worker_task:
            self.worker_task.cancel()
            try: await self.worker_task
            except asyncio.CancelledError: pass
        await self.media.close()
        await self.ai.close()
        self.store.close()

async def run():
    client = TaskBot(botpy.Intents(public_messages=True))
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    try:
        loop.add_signal_handler(signal.SIGTERM, stop.set)
    except (NotImplementedError, RuntimeError):
        log.info("sigterm_handler_unavailable")

    async def connect():
        delay = 5
        while not stop.is_set():
            try:
                await client.start(os.environ["QQ_APPID"], os.environ["QQ_SECRET"])
                delay = 5
            except Exception as exc:
                log.warning("gateway_disconnected type=%s retry_in=%s", type(exc).__name__, delay)
            if not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except TimeoutError:
                    delay = min(delay * 2, 60)

    gateway = asyncio.create_task(connect(), name="qq-gateway")
    try:
        await stop.wait()
    finally:
        gateway.cancel()
        try:
            await gateway
        except asyncio.CancelledError:
            pass
        await client.shutdown()


def main():
    try:
        validate_environment()
    except ValueError as exc:
        raise SystemExit("配置错误：" + str(exc))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", force=True)
    asyncio.run(run())


if __name__ == "__main__":
    main()
