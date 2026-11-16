"""Submission, attachment collection and execution for the QQ task worker."""
from __future__ import annotations

import asyncio
import logging

from ai import AIError
from input_cache import InputCacheError, cache_attachments, discard_cached_attachments
from message_input import _is_pdf
from pdf_tools import PDFError, explicit_pdf_selection
from solver import STUDY_ACTIONS, InputError
from task_store import ACTIVE, QueueFull, describe

log = logging.getLogger("qq-math")


class TaskQueue:
    """Queue operations used by TaskBot and the command handlers."""

    def _registration_receipt(self, tid, action=None):
        if action in STUDY_ACTIONS:
            return f"已登记 {tid}，正在排队{action}。完成后会回复文字，也可发送 {tid} 领取。"
        return (f"已登记 {tid}。\n按顺序处理，单题运行最多 {self.settings.job_timeout//60} 分钟，排队另计。\n"
                f"直接发送 {tid} 可查询进度或领取结果。\n"
                "完成后：1–3 页发全部图片，超过 3 页发完整 PDF。解答和复核状态会分别标明。\n"
                "发送 /帮助 查看机器人全部可用指令")

    async def _create_collection(self, context, text, quoted, attachments, *, parent_id=""):
        if parent_id:
            parent = self.store.get(parent_id, context.owner)
            if not parent or parent["state"] == "expired":
                await self._text(context, "未找到你在本会话中可用的原题号，请核对后再收集补充材料。")
                return
        if len(text) + len(quoted) > 24000:
            await self._text(context, "收集材料超过 2.4 万字符，请按题目拆分。")
            return
        try:
            selection = explicit_pdf_selection(text) if any(_is_pdf(item) for item in attachments) else ""
        except PDFError as exc:
            await self._text(context, str(exc))
            return
        async with self.collection_lock:
            self.store.cleanup(self.settings.result_ttl)
            if self.store.disk_bytes() >= self.settings.disk_limit_mb * 1024**2:
                await self._text(context, "材料缓存已达到容量预算，请稍后再提交。")
                return
            request = {"text": "" if selection else text,
                       "quoted": quoted, "attachments": [], "collected": True}
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
                    updated, added = await self._save_collection_materials(context, task, selection, "", attachments)
                except (InputCacheError, ValueError) as exc:
                    await self._text(context, f"{tid} 首批附件未保存：{exc}\n请用 /补图 {tid} 重新发送这些附件后再开始。")
                    return
                await self._text(context, f"{tid} 已保存本次材料（新增 {added} 个附件）。\n" + describe(updated))

    async def _save_collection_materials(self, context, task, text, quoted, attachments):
        """Save one batch while the caller holds collection_lock."""
        tid = task["task_id"]
        cached = []
        try:
            if any(_is_pdf(item) for item in attachments):
                try:
                    selection = explicit_pdf_selection(text)
                except PDFError as exc:
                    raise ValueError(str(exc)) from exc
                if selection:
                    text = ""
                    attachments = [{**item, "pdf_selection": selection} if _is_pdf(item) else item
                                   for item in attachments]
            with self.store.retain_result(tid):
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
                self.store.start_collection(tid, context.owner, max_active=self.settings.max_queue,
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
            if request.get("reuse_key"):
                await self._deliver(task, context)
            else:
                await self._text(context, describe(task))
            return
        self.contexts[tid] = context
        # Acknowledge before allowing a fast task to consume this message's reply sequence.
        await self._text(context, self._registration_receipt(tid, request.get("action")))
        try:
            self.queue.put_nowait(tid)
        except asyncio.QueueFull:
            self.contexts.pop(tid, None)
            self.store.update(tid, state="failed", error="队列正在释放取消的任务，请稍后按题号重试。")
            await self._text(context, describe(self.store.get(tid)))

    async def _cancel_task(self, context, task):
        tid = task["task_id"]
        if task["state"] == "collecting":
            async with self.collection_lock:
                current = self.store.get(tid, context.owner)
                if current["state"] == "collecting":
                    self.store.update(tid, state="cancelled", error="用户取消材料收集")
                    await self._text(context, f"{tid} 已取消材料收集。")
                else:
                    await self._text(context, "题目状态已变化，请重新查询后取消。")
            return
        if task["state"] not in ACTIVE:
            await self._text(context, "该题当前没有运行中的任务。")
            return
        self.store.update(tid, state="cancelled", error="用户取消")
        if self.running_id == tid and self.running_task:
            self.running_task.cancel()
        await self._text(context, f"{tid} 已取消。")

    async def _retry_task(self, context, task):
        tid = task["task_id"]
        if task["state"] == "collecting":
            await self._text(context, f"这道题尚未开始解答，请发 /开始 {tid}，无需重试。")
        elif task["state"] in ACTIVE:
            await self._text(context, "该题仍在处理中，无需重复提交。")
        elif task["state"] == "expired":
            await self._text(context, "题目材料已过期，请重新发送。")
        elif task["request"].get("action") == "提示":
            await self._text(context, "提示指令已下线，请直接发送题目获取解答。")
        else:
            request = {**task["request"], "retry_of": tid}
            if self.store.get_favorite(tid, context.owner):
                request["favorite_id"] = tid
            await self._submit(context, request)

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
            try:
                async with self.resource_lock:
                    task = self.store.get(tid)
                    if not task or task["state"] != "queued":
                        continue
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
