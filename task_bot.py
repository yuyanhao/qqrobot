"""QQ problem tasks: immediate ID, persistent status, PDF, and scoped follow-ups."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re
import time
from urllib.parse import urlparse

import botpy
from botpy.http import Route
from botpy.message import BaseMessage

from ai import AIError, DeepSeek
from message_input import clean_text, collect_attachments
from input_cache import InputCacheError, cache_attachments, discard_cached_attachments
from qqmedia import QQMedia, QQMediaError, qq_api_base_url
from render_service import render_preview_async
from settings import Settings, validate_environment
from solver import InputError, Solver
from task_store import ACTIVE, TASK_RE, QueueFull, TaskStore, describe

log = logging.getLogger("qq-math")
RAW_EVENTS = OrderedDict()
_original_message_init = BaseMessage.__init__


def _message_init(self, api, event_id, data):
    _original_message_init(self, api, event_id, data)
    if data.get("id"):
        RAW_EVENTS[data["id"]] = data
        while len(RAW_EVENTS) > 300: RAW_EVENTS.popitem(last=False)


BaseMessage.__init__ = _message_init


@dataclass
class ReplyContext:
    kind: str
    target: str
    owner: str
    message_id: str
    received: float
    seq: int = 0

    def passive_available(self):
        return (time.time() - self.received < (290 if self.kind == "group" else 3585)
                and self.seq < (5 if self.kind == "group" else 4))


HELP = ("我只处理数学物理题及题目追问。直接发送题目/图片/PDF 即可获得题号。\n"
        "PDF 默认读取全部页面；随文件写“只读第3-5页”或“第2、5、8-10页”可指定页码（从1开始）。\n"
        "完成后：1–3 页发全部图片，超过 3 页发完整 PDF。\n"
        "群聊每条操作都需 @ 机器人；请用提交原题的账号在原会话操作。\n\n"
        "题号 或 /结果 题号：查询状态，完成后领取结果\n"
        "/PDF 题号：领取完整 PDF，也支持 /pdf\n"
        "/进度 题号：只看进度和复核状态\n"
        "/图片 题号 1：从第 1 页取最多两页图片\n"
        "/文字 题号 1：取第 1 段文字，每段最多 1500 字符\n"
        "/追问 题号 具体问题：关联原题继续提问\n"
        "/补充 题号 补充条件：可用文字或同条附图补充条件\n"
        "/收集 [说明或原题号]：先收齐多张题图，暂不解题\n"
        "/补图 收集题号：同条附图或引用图片，分批追加材料\n"
        "/开始 收集题号：收齐后开始解题，沿用这个题号\n"
        "/取消 题号：取消收集中、排队或运行中的任务\n"
        "/重试 题号：重新解题，创建新题号，可能产生 API 费用\n"
        "/题目：查看本会话中自己的最近 5 题\n"
        "/帮助：查看完整用法\n\n"
        "图片起始页、文字段号省略时默认为 1。追问、补充均可只附图片，无须另写文字。"
        "多张补充图可先 /收集 原题号，再逐条 /补图，最后 /开始。"
        "每条最多 4 个附件，收集题累计最多 12 个；收集期间不会调用模型。"
        "QQ 没有提供引用附件时请重新发送原图。支持全角斜杠 ／。\n"
        "联网仅在解题需要时使用，不提供普通聊天、新闻搜索或通用文件处理。")


class TaskBot(botpy.Client):
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
        self.ai = DeepSeek(os.environ["DEEPSEEK_API_KEY"], os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                           os.getenv("DEEPSEEK_MODEL", "deepseek-flash"), os.getenv("DEEPSEEK_REASONING_EFFORT", "high"),
                           vision_model=os.getenv("VISION_MODEL", "deepseek-flash"))
        self.media = QQMedia(os.environ["QQ_APPID"], os.environ["QQ_SECRET"], api_base_url,
                             active_delivery=self.settings.active_delivery)
        self.solver = Solver(self.store, self.ai, self.settings)
        self.queue = asyncio.Queue(maxsize=self.settings.max_queue)
        self.resource_lock = asyncio.Lock()
        self.collection_lock = asyncio.Lock()
        self.worker_task = None
        self.running_task = None
        self.running_id = None
        self.contexts = {}
        self.seen = OrderedDict()
        self.allowed_groups = {x.strip() for x in os.getenv("ALLOWED_GROUPS", "").split(",") if x.strip()}
        self.allowed_users = {x.strip() for x in os.getenv("ALLOWED_USERS", "").split(",") if x.strip()}

    def _ensure_worker(self):
        if self.worker_task is None or self.worker_task.done():
            self.worker_task = asyncio.create_task(self._worker(), name="problem-worker")

    async def on_ready(self):
        self._ensure_worker()
        log.info("problem_bot_ready")

    async def on_group_at_message_create(self, message):
        group, user = message.group_openid, message.author.member_openid
        if self.allowed_groups and group not in self.allowed_groups: return
        if not user: return
        await self._handle(message, "group", group, f"group:{group}:{user}")

    async def on_c2c_message_create(self, message):
        user = message.author.user_openid
        if not user or (self.allowed_users and user not in self.allowed_users): return
        await self._handle(message, "c2c", user, f"c2c:{user}")

    async def _send(self, context, method, *args):
        if context.passive_available():
            context.seq += 1
            await method(context.kind, context.target, *args, context.message_id, context.seq)
        elif self.settings.active_delivery:
            await method(context.kind, context.target, *args, None, None)
        else:
            raise QQMediaError("被动回复窗口或次数已用完，请用题号重新领取")

    async def _text(self, context, text):
        try:
            await self._send(context, self.media.send_text, text[:1800])
            return True
        except Exception as exc:
            log.warning("text_delivery_failed type=%s", type(exc).__name__)
            return False

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
        text = clean_text(getattr(message, "content", "") or "").strip()
        text = re.sub(r"^[/／]\s*", "/", text)
        quoted, attachments = collect_attachments(message, raw)
        if text in ("/帮助", "帮助", "/help", "你好", "您好"):
            await self._text(context, HELP)
            return
        if text in ("/题目", "题目", "我的题目"):
            recent = self.store.list_recent(owner)
            await self._text(context, "\n\n".join(describe(t) for t in recent) or "还没有题目记录。")
            return
        collection = re.fullmatch(r"/?收集(?:\s+([\s\S]+))?", text)
        if collection:
            await self._create_collection(context, (collection.group(1) or "").strip(), quoted, attachments)
            return
        match = re.fullmatch(r"/?(?:(进度|结果|图片|文字|取消|重试|追问|补充|补图|开始|PDF)\s+)?#?(" + TASK_RE + r")(?:\s+([\s\S]+))?", text, re.I)
        if match:
            action, tid, tail = match.group(1) or "结果", match.group(2).upper(), (match.group(3) or "").strip()
            if action.casefold() == "pdf": action = "PDF"
            task = self.store.get(tid, owner)
            if task is None:
                await self._text(context, "未找到你在本会话中的这个题号。")
                return
            if action == "补图" or (action == "补充" and task["state"] == "collecting"):
                if task["state"] != "collecting":
                    await self._text(context, f"/补图 仅用于收集中的题目；多张补充图请先发 /收集 {tid}，单次补充可发 /补充 {tid} 并附图。")
                    return
                await self._append_collection(context, tid, tail, quoted, attachments)
            elif action == "开始":
                if tail or attachments or quoted:
                    await self._text(context, f"请先用 /补图 {tid} 追加材料，再单独发送 /开始 {tid}。")
                else:
                    await self._start_collection(context, tid)
            elif action in ("追问", "补充") or (action == "结果" and tail):
                if task["state"] == "collecting":
                    await self._text(context, f"这道题还在收集材料，请先发 /开始 {tid}；补充材料可用 /补图 或 /补充。")
                    return
                if not tail and not quoted and not attachments:
                    await self._text(context, "请填写补充条件/追问，或在同一条消息附上图片。QQ 未提供引用图片时，请重新发送原图。")
                    return
                await self._submit(context, {"text": tail, "parent_id": tid, "quoted": quoted,
                                             "attachments": attachments,
                                             "action": "补充" if action == "补充" else "追问"})
            elif action == "取消":
                if task["state"] == "collecting":
                    async with self.collection_lock:
                        current = self.store.get(tid, owner)
                        if current["state"] == "collecting":
                            self.store.update(tid, state="cancelled", error="用户取消材料收集")
                            await self._text(context, f"{tid} 已取消材料收集。")
                        else:
                            await self._text(context, "题目状态已变化，请重新查询后取消。")
                elif task["state"] not in ACTIVE:
                    await self._text(context, "该题当前没有运行中的任务。")
                else:
                    self.store.update(tid, state="cancelled", error="用户取消")
                    if self.running_id == tid and self.running_task: self.running_task.cancel()
                    await self._text(context, f"{tid} 已取消。")
            elif action == "重试":
                if task["state"] == "collecting":
                    await self._text(context, f"这道题尚未开始解答，请发 /开始 {tid}，无需重试。")
                elif task["state"] in ACTIVE:
                    await self._text(context, "该题仍在处理中，无需重复提交。")
                elif task["state"] == "expired":
                    await self._text(context, "题目材料已过期，请重新发送。")
                else:
                    await self._submit(context, {**task["request"], "retry_of": tid})
            elif action == "PDF":
                if tail:
                    await self._text(context, f"用法：/PDF {tid}（不需要页码或其他参数）")
                else:
                    await self._deliver(task, context, "PDF")
            elif action in ("图片", "文字"):
                try: page = int(tail or "1")
                except ValueError: page = 0
                if page < 1:
                    await self._text(context, "页码/段号必须为正整数。")
                else:
                    await self._deliver(task, context, action, page)
            elif action == "进度": await self._text(context, describe(task))
            else: await self._deliver(task, context)
            return
        if text.startswith(("/搜", "/问", "/读", "/文件")):
            await self._text(context, "通用聊天、搜索和文件命令已关闭。请直接发送数学物理题，解题时会按需搜索。")
            return
        if text.startswith("/"):
            await self._text(context, HELP)
            return
        if not text and not quoted and not attachments:
            await self._text(context, HELP)
            return
        if len(text) + len(quoted) > 24000:
            await self._text(context, "题目超过 2.4 万字符，请按小问拆分。")
            return
        await self._submit(context, {"text": text, "quoted": quoted, "attachments": attachments})

    def _registration_receipt(self, tid):
        return (f"已登记 {tid}。\n按顺序处理，单题运行最多 {self.settings.job_timeout//60} 分钟，排队另计。\n"
                f"直接发送 {tid} 可查询进度或领取结果。\n"
                "完成后：1–3 页发全部图片，超过 3 页发完整 PDF。解答和复核状态会分别标明。\n"
                "发送 /帮助 查看机器人全部可用指令")

    async def _create_collection(self, context, text, quoted, attachments):
        parent_id = ""
        parent_match = re.fullmatch(r"#?(" + TASK_RE + r")(?:\s+([\s\S]+))?", text, re.I)
        if parent_match:
            parent_id = parent_match.group(1).upper()
            parent = self.store.get(parent_id, context.owner)
            if not parent or parent["state"] == "expired":
                await self._text(context, "未找到你在本会话中可用的原题号，请核对后再收集补充材料。")
                return
            text = (parent_match.group(2) or "").strip()
        if len(text) + len(quoted) > 24000:
            await self._text(context, "收集材料超过 2.4 万字符，请按题目拆分。")
            return
        async with self.collection_lock:
            self.store.cleanup(self.settings.result_ttl)
            if self.store.disk_bytes() >= self.settings.disk_limit_mb * 1024**2:
                await self._text(context, "材料缓存已达到容量预算，请稍后再提交。")
                return
            request = {"text": text, "quoted": quoted, "attachments": [], "collected": True}
            if parent_id:
                request.update(parent_id=parent_id, action="补充")
            try:
                task, created = self.store.create(context.owner, context.message_id, context.kind, context.target, request,
                    self.settings.max_queue, self.settings.max_user_tasks, self.settings.max_daily, state="collecting")
            except QueueFull as exc:
                await self._text(context, str(exc))
                return
            if not created:
                await self._text(context, describe(task))
                return
            tid = task["task_id"]
            if attachments:
                # Persist this before the first reply: a concurrent /开始 or a
                # restart must not submit only the text while photos are pending.
                self.store.update(tid, error="首批附件未保存，请重新发送这些附件后再开始。")
            await self._text(context, f"已登记 {tid}，正在收集材料，暂未开始解题。\n"
                             f"每条发送 /补图 {tid} 并附图片；收齐后发送 /开始 {tid}。\n"
                             "每条最多 4 个附件，累计最多 12 个。\n发送 /帮助 查看机器人全部可用指令")
            if attachments:
                try:
                    updated, added = await self._save_collection_materials(context, task, "", "", attachments)
                except (InputCacheError, ValueError) as exc:
                    await self._text(context, f"{tid} 首批附件未保存：{exc}\n请用 /补图 {tid} 重新发送这些附件后再开始。")
                    return
                await self._text(context, f"{tid} 已保存本次材料（新增 {added} 个附件）。\n" + describe(updated))

    async def _save_collection_materials(self, context, task, text, quoted, attachments):
        """Save one batch while the caller holds collection_lock."""
        tid = task["task_id"]
        cached = []
        try:
            cached = await cache_attachments(self.store, task, attachments, max_total_mb=self.settings.disk_limit_mb)
            if attachments and not cached and not text and not quoted:
                # A supplied image can already exist under the same URL or hash.
                # It is a successful no-op, not an empty /补图 request.
                updated, added = task, 0
            else:
                updated, added = self.store.append_materials(tid, context.owner, text=text, quoted=quoted,
                    attachments=cached, message_id=context.message_id)
        except BaseException:
            discard_cached_attachments(self.store, task, cached)
            raise
        kept = {item.get("cached_name") for item in updated["request"].get("attachments", [])}
        discard_cached_attachments(self.store, task, [item for item in cached if item["cached_name"] not in kept])
        if attachments and self.store.get(tid, context.owner)["error"].startswith("首批附件未保存"):
            self.store.update(tid, error="")
            updated = self.store.get(tid, context.owner)
        return updated, added

    async def _append_collection(self, context, tid, text, quoted, attachments):
        if not text and not quoted and not attachments:
            await self._text(context, f"请在 /补图 {tid} 同一条消息中附上图片。QQ 未提供引用图片时请重新发送原图。")
            return
        async with self.collection_lock:
            task = self.store.get(tid, context.owner)
            if not task or task["state"] != "collecting":
                await self._text(context, "题目已不在收集状态，未追加材料。")
                return
            try:
                updated, added = await self._save_collection_materials(context, task, text, quoted, attachments)
            except (InputCacheError, ValueError) as exc:
                await self._text(context, f"{tid} 材料未追加：{exc}\n原有材料仍保留，可继续 /补图。")
                return
        await self._text(context, f"{tid} 已保存本次材料（新增 {added} 个附件）。\n" + describe(updated))

    async def _start_collection(self, context, tid):
        async with self.collection_lock:
            if self.queue.full():
                await self._text(context, f"队列已满，{tid} 的材料仍保留，请稍后再发 /开始 {tid}。")
                return
            try:
                task = self.store.start_collection(tid, context.owner, max_active=self.settings.max_queue,
                                                   max_user_active=self.settings.max_user_tasks)
            except (ValueError, QueueFull) as exc:
                await self._text(context, str(exc))
                return
            self.contexts[tid] = context
            await self._text(context, self._registration_receipt(tid))
            try:
                self.queue.put_nowait(tid)
            except asyncio.QueueFull:
                self.contexts.pop(tid, None)
                self.store.update(tid, state="collecting")
                await self._text(context, f"队列暂满，材料已保留，请稍后再发 /开始 {tid}。")

    async def _submit(self, context, request):
        self.store.cleanup(self.settings.result_ttl)
        if self.store.disk_bytes() > self.settings.disk_limit_mb * 1024**2:
            await self._text(context, "结果存储已达到容量预算，请管理员清理过期文件或调整预算。")
            return
        try:
            task, created = self.store.create(context.owner, context.message_id, context.kind, context.target,
                request, self.settings.max_queue, self.settings.max_user_tasks, self.settings.max_daily)
        except QueueFull as exc:
            await self._text(context, str(exc))
            return
        tid = task["task_id"]
        if not created:
            await self._text(context, describe(task))
            return
        self.contexts[tid] = context
        # Acknowledge before allowing a fast task to consume this message's reply sequence.
        await self._text(context, self._registration_receipt(tid))
        try:
            self.queue.put_nowait(tid)
        except asyncio.QueueFull:
            self.contexts.pop(tid, None)
            self.store.update(tid, state="failed", error="队列正在释放取消的任务，请稍后按题号重试。")
            await self._text(context, describe(self.store.get(tid)))

    def _finish_failure(self, tid, error):
        row = self.store.get(tid)
        result = row["result"]
        if result.get("text_path"):
            result.update(answer_status="uncertain", verification="未完成（后续处理中断）")
            if result.get("solution"):
                result["solution"]["status"] = "uncertain"
            self.store.update(tid, state="completed", result=result,
                              error=error + " 已保留文字解答，尚不能视为已解决。")
        else:
            self.store.update(tid, state="failed", error=error)

    async def _worker(self):
        while True:
            tid = await self.queue.get()
            task = self.store.get(tid)
            try:
                if not task or task["state"] != "queued": continue
                async with self.resource_lock:
                    self.running_id = tid
                    self.running_task = asyncio.create_task(self.solver.run(task))
                    try:
                        await asyncio.wait_for(self.running_task, timeout=self.settings.job_timeout)
                    except asyncio.CancelledError:
                        if asyncio.current_task().cancelling() or self.store.get(tid)["state"] != "cancelled": raise
                    except TimeoutError:
                        self._finish_failure(tid, "总时间预算已到，可按题号重试或拆成小问。")
                    except AIError as exc:
                        self._finish_failure(tid, f"{exc.stage or '模型调用'}失败 [{exc.code}]：{exc}")
                        log.warning("task_model_failed task=%s stage=%s code=%s", tid, exc.stage, exc.code)
                    except InputError as exc:
                        self.store.update(tid, state="needs_input", error=str(exc))
                    except Exception as exc:
                        self._finish_failure(tid, f"处理失败 [{type(exc).__name__}]，题目已保存，可重试。")
                        log.warning("task_failed task=%s type=%s", tid, type(exc).__name__)
                    finally:
                        self.running_task, self.running_id = None, None
                context = self.contexts.get(tid)
                if context and (context.passive_available() or self.settings.active_delivery):
                    try:
                        await self._deliver(self.store.get(tid), context)
                    except Exception as exc:
                        self.store.update(tid, delivery="awaiting_query")
                        log.warning("delivery_interrupted task=%s type=%s", tid, type(exc).__name__)
                else:
                    self.store.update(tid, delivery="awaiting_query")
            finally:
                self.contexts.pop(tid, None)
                self.queue.task_done()

    async def _deliver(self, task, context, mode="结果", page=1):
        tid, result = task["task_id"], task["result"]
        if task["state"] not in ("completed", "needs_input") or not result:
            await self._text(context, describe(task))
            return
        if mode == "文字":
            path = Path(result["text_path"]) if result.get("text_path") else None
            if not path or not path.is_file():
                await self._text(context, f"{tid} 没有可用的文字文件，请查询进度或重新提交题目。")
                return
            text = path.read_text(encoding="utf-8")
            chunks = [text[i:i+1500] for i in range(0, len(text), 1500)]
            await self._text(context, f"{tid} 文字 {page}/{len(chunks)}\n" +
                             (chunks[page-1] if page <= len(chunks) else "没有这一段。"))
            return
        if not context.passive_available() and not self.settings.active_delivery:
            self.store.update(tid, delivery="awaiting_query")
            return
        try:
            path = Path(result["pdf_path"]) if result.get("pdf_path") else None
            if not path or not path.is_file():
                self.store.update(tid, delivery="awaiting_query")
                await self._text(context, describe(task) + ("\n" + result["notes"] if result.get("notes") else "") +
                                 f"\n没有可用 PDF，可能尚未生成或排版未完成。完整文字可发 /文字 {tid} 1 领取。")
                return

            total = result.get("total_pages")
            if mode != "PDF" and (not isinstance(total, int) or total < 1):
                # Older saved results may have a PDF but no cached page count.
                if self.resource_lock.locked():
                    self.store.update(tid, delivery="awaiting_query")
                    await self._text(context, f"当前正在处理题目，暂缓读取页数；可发 /PDF {tid} 领取完整 PDF。")
                    return
                async with self.resource_lock:
                    batch = await render_preview_async(path, self.store.task_dir(tid), 1, 3)
                result.update(total_pages=batch["total_pages"], preview_paths=batch["preview_paths"])
                self.store.update(tid, result=result)
                total = result["total_pages"]

            image_mode = mode == "图片" or (mode == "结果" and total <= 3)
            existing = []
            status = describe(task) + ("\n" + result["notes"] if result.get("notes") else "")
            if image_mode:
                if mode == "结果": page = 1
                if not 1 <= page <= total:
                    await self._text(context, f"{tid} 共 {total} 页，没有第 {page} 页。")
                    return
                count = 2 if mode == "图片" else total
                numbers = list(range(page, min(page + count, total + 1)))
                existing = [self.store.task_dir(tid) / f"preview-{number:03d}.png" for number in numbers]
                if not all(p.is_file() for p in existing):
                    if self.resource_lock.locked():
                        self.store.update(tid, delivery="awaiting_query")
                        await self._text(context, f"当前正在处理题目，暂缓生成新预览；可发 /PDF {tid} 领取完整 PDF。")
                        return
                    async with self.resource_lock:
                        batch = await render_preview_async(path, self.store.task_dir(tid), page, count)
                    existing = [Path(p) for p in batch["preview_paths"]]
                    if len(existing) != len(numbers):
                        raise QQMediaError("预览页面不完整，请重新领取")
                status += f"\n图片：第 {numbers[0]}–{numbers[-1]} 页，共 {total} 页。"
                if numbers[-1] < total:
                    status += f"\n继续领取：/图片 {tid} {numbers[-1] + 1}"

            required = len(existing) if image_mode else 1
            room = (5 if context.kind == "group" else 4) - context.seq if context.passive_available() else 0
            if not self.settings.active_delivery and room < required:
                self.store.update(tid, delivery="awaiting_query")
                await self._text(context, f"结果已保存，本条消息剩余回复次数不足。请重新发送 {tid} 或 /PDF {tid} 领取。")
                return
            # An automatic three-page C2C answer has exactly three slots left
            # after its acknowledgement. Verification remains in the page images.
            if self.settings.active_delivery or room >= required + 1:
                if not await self._text(context, status):
                    self.store.update(tid, delivery="awaiting_query")
                    return

            if image_mode:
                for path in existing:
                    info = await self.media.upload_image(context.kind, context.target, path.read_bytes(), path.name)
                    await self._send(context, self.media.send_image, info)
            else:
                info = await self.media.upload_file_path(context.kind, context.target, path)
                await self._send(context, self.media.send_file, info)
            self.store.update(tid, delivery="delivered")
        except Exception as exc:
            self.store.update(tid, delivery="awaiting_query")
            log.warning("result_delivery_failed task=%s type=%s", tid, type(exc).__name__)
            await self._text(context, f"结果已保存，但此次发送未成功。请重新发送 {tid}，或发 /PDF {tid}、/图片 {tid} 1、/文字 {tid} 1 领取。")

    async def shutdown(self):
        if self.worker_task:
            self.worker_task.cancel()
            try: await self.worker_task
            except asyncio.CancelledError: pass
        await self.media.close()
        await self.ai.close()
        self.store.close()


async def run():
    client = TaskBot(botpy.Intents(public_messages=True))
    try:
        while True:
            try:
                await client.start(os.environ["QQ_APPID"], os.environ["QQ_SECRET"])
            except Exception as exc:
                log.warning("gateway_disconnected type=%s", type(exc).__name__)
            await asyncio.sleep(5)
    finally:
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
