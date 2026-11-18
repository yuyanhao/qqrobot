"""Submission, attachment collection and execution for the QQ task worker."""
from __future__ import annotations

import asyncio
from contextlib import aclosing
import logging
import re
import shutil

from ai import AIError
from input_cache import (InputCacheError, cache_attachments, discard_cached_attachments,
                         read_cached_attachment)
from message_input import _is_pdf, _is_text_file
from pdf_tools import (MAX_INPUT_PAGES, PDFError,
                       pdf_pages_from_text, pdf_selection_page_count,
                       pdf_task_page_budget, split_pdf_selection)
from render_service import render_input_pdf_batches_async
from solver import STUDY_ACTIONS, InputError, normalize_image, study_problem
from task_store import ACTIVE, QueueFull, describe

log = logging.getLogger("qq-math")

CHECK_ROLE_TOKEN = re.compile(r"(\d{1,2})(?:\s*[-–—~～至到]\s*(\d{1,2}))?\s*(题|稿|同页)")
CHECK_ROLES = {"题": "question", "稿": "draft", "同页": "both"}


def parse_check_role_spec(text, count):
    """Parse ``1题 2-4稿`` style role hints. Return None when text is not a spec."""
    text = (text or "").strip()
    if not text or count < 1:
        return None
    tokens = list(CHECK_ROLE_TOKEN.finditer(text))
    if not tokens:
        return None
    position, roles = 0, {}
    separators = " \t,，、;；"
    for match in tokens:
        if text[position:match.start()].strip(separators):
            return None
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if not 1 <= start <= end <= count:
            raise ValueError(f"角色指定超出材料数量 1-{count}，请写例如“1题 2-4稿”。")
        role = CHECK_ROLES[match.group(3)]
        for index in range(start, end + 1):
            if index in roles:
                raise ValueError(f"第 {index} 份材料被重复指定角色。")
            roles[index] = role
        position = match.end()
    if text[position:].strip(separators):
        return None
    missing = [str(index) for index in range(1, count + 1) if index not in roles]
    if missing:
        raise ValueError(f"请为全部 {count} 份材料指定角色，缺少第 {'、'.join(missing)} 份。")
    return [roles[index] for index in range(1, count + 1)]



class TaskQueue:
    """Queue operations used by TaskBot and the command handlers."""

    @staticmethod
    def _estimated_ocr_batches(request):
        question_attachments = request.get("question_attachments") or []
        try:
            text_pages = len(pdf_pages_from_text(request.get("text") or ""))
        except PDFError:
            text_pages = 0

        def estimate_role(attachments):
            pending_images = 0
            batches = 0
            for item in attachments:
                if item.get("book_id") or _is_pdf(item):
                    if pending_images:
                        batches += (pending_images + 3) // 4
                        pending_images = 0
                    selection = item.get("pdf_selection")
                    if selection:
                        try:
                            pages = pdf_selection_page_count(selection)
                        except PDFError:
                            pages = 0
                    else:
                        pages = text_pages
                    batches += (pages + 3) // 4
                elif _is_text_file(item):
                    if pending_images:
                        batches += (pending_images + 3) // 4
                        pending_images = 0
                else:
                    pending_images += 1
            if pending_images:
                batches += (pending_images + 3) // 4
            return batches

        batches = estimate_role(question_attachments)
        batches += estimate_role(request.get("attachments") or [])
        return batches

    @staticmethod
    def _pdf_page_budget(request):
        """Validate page selections and the whole-task page budget before queueing."""
        pdfs = [item for item in [*(request.get("question_attachments") or []),
                                  *(request.get("attachments") or [])]
                if _is_pdf(item) or item.get("book_id")]
        return pdf_task_page_budget(pdfs, request.get("text") or "")

    def _registration_receipt(self, tid, action=None, request=None):
        estimated = self._estimated_ocr_batches(request or {}) if request else 0
        estimate = f"预计识图约 {estimated} 批（每批最多 4 页/图）。\n" if estimated else ""
        if action in STUDY_ACTIONS:
            return f"已登记 {tid}，正在排队{action}。\n{estimate}完成后会回复文字，也可发送 {tid} 领取。"
        return (f"已登记 {tid}。\n按顺序处理，单题运行最多 {self.settings.job_timeout//60} 分钟，排队另计。\n"
                f"{estimate}直接发送 {tid} 可查询进度或领取结果。\n"
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
            has_pdf = any(_is_pdf(item) for item in attachments)
            selection, remaining_text = split_pdf_selection(text) if has_pdf else ("", text)
            if has_pdf and not selection:
                raise PDFError(f"附带 PDF 时请明确指定页码；每份 PDF 每次最多读取 {MAX_INPUT_PAGES} 页，例如“第3页”或“第3-5页”。")
        except PDFError as exc:
            await self._text(context, str(exc))
            return
        async with self.collection_lock:
            self.store.cleanup(self.settings.result_ttl)
            if self.store.disk_bytes() >= self.settings.disk_limit_mb * 1024**2:
                await self._text(context, "材料缓存已达到容量预算，请稍后再提交。")
                return
            request = {"text": remaining_text,
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
                # Persist this before the first reply: a concurrent /解题 or
                # restart must not submit only the text while photos are pending.
                self.store.update(tid, error="首批附件未保存，请重新发送这些附件后再开始。")
            await self._text(context, f"已登记 {tid}，正在收集材料，暂未处理。\n"
                             f"每条发送 /补图 {tid} 并附图片；收齐后发送 /解题 {tid} 或 /查错 {tid}。\n"
                             "每条最多 4 个附件，累计最多 12 个。\n发送 /帮助 查看机器人全部可用指令")
            if attachments:
                try:
                    updated, added = await self._save_collection_materials(
                        context, task, remaining_text, "", attachments, selection=selection)
                except (InputCacheError, ValueError) as exc:
                    await self._text(context, f"{tid} 首批附件未保存：{exc}\n请用 /补图 {tid} 重新发送这些附件后再开始。")
                    return
                await self._text(context, f"{tid} 已保存本次材料（新增 {added} 个附件）。\n" + describe(updated))

    async def _save_collection_materials(self, context, task, text, quoted, attachments, *, selection=None):
        """Save one batch while the caller holds collection_lock."""
        tid = task["task_id"]
        cached = []
        try:
            if any(_is_pdf(item) for item in attachments):
                try:
                    if selection is None:
                        selection, text = split_pdf_selection(text)
                except PDFError as exc:
                    raise ValueError(str(exc)) from exc
                if not selection:
                    raise ValueError(f"附带 PDF 时请明确指定页码；每份 PDF 每次最多读取 {MAX_INPUT_PAGES} 页。")
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
        current = self.store.get(tid, context.owner)
        if attachments and current and current["error"].startswith("首批附件未保存"):
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

    async def _start_collection_check(self, context, task, tail, attachments):
        tid = task["task_id"]
        if attachments:
            await self._text(context, f"查错材料请先用 /补图 {tid} 追加，再发 /查错 {tid}。")
            return
        current = self.store.get(tid, context.owner)
        if not current or current["state"] != "collecting":
            await self._text(context, f"{tid} 已开始或不在收集状态，请按题号查询进度。")
            return
        request = dict(current["request"])
        items = list(request.get("attachments") or [])
        if not items:
            await self._text(context, f"{tid} 还没有材料，请先发送 /补图 {tid} 并附图片或 PDF。")
            return

        parent = None
        if request.get("parent_id"):
            candidate = self.store.get(request["parent_id"], context.owner)
            if candidate and candidate["state"] != "expired" and candidate["problem"]:
                parent = candidate
            else:
                await self._text(
                    context,
                    "关联的原题还没有可用题干或已过期；请等识题完成后再 /查错，"
                    f"或 /取消 {tid} 后重新 /收集。")
                return
        if parent:
            if tail:
                await self._text(context, "该收集已关联原题，材料会自动按草稿处理；不需要再指定角色。")
                return
            if parent["question_ocr"].get("needs_confirmation"):
                await self._text(context, f"原题识图有疑点，请先发 /题干 {parent['task_id']} 核对。")
                return
            problem_snapshot = study_problem(parent["problem"], parent["request"])
            question_ocr = parent["question_ocr"]
            roles = ["draft"] * len(items)
            note = "已使用关联原题的题干，收集材料全部按草稿处理。"
        else:
            if tail.strip() == "同页" and len(items) == 1:
                roles = ["both"]
                note = "已按同页处理唯一的收集材料。"
            elif tail:
                try:
                    roles = parse_check_role_spec(tail, len(items))
                except ValueError as exc:
                    await self._text(context, str(exc))
                    return
                if roles is None:
                    await self._text(
                        context,
                        f"参数只用于指定角色，例如 /查错 {tid} 1题 2-4稿；"
                        "草稿文字请写在 /收集 或 /补图 的说明里。")
                    return
                note = "已按你指定的角色拆分材料。"
            else:
                roles, note = await self._auto_check_roles(context, current, items)
                if roles is None:
                    return
            problem_snapshot, question_ocr = "", {}
            if not any(role in ("question", "both") for role in roles):
                await self._text(context, f"{tid} 没有识别到题目材料；请用 /查错 {tid} 1题 2-4稿 指定。")
                return
            if not any(role in ("draft", "both") for role in roles):
                await self._text(
                    context,
                    f"{tid} 没有识别到草稿材料；如果题目和作答在同一张图，请用 /查错 {tid} 1同页 指定。")
                return

        question_items = [item for item, role in zip(items, roles) if role in ("question", "both")]
        draft_items = [item for item, role in zip(items, roles) if role in ("draft", "both")]
        new_request = {
            **request, "action": "查错",
            "question_attachments": question_items, "attachments": draft_items,
            "problem_snapshot": problem_snapshot, "question_ocr": question_ocr,
        }
        for key in ("check_flow", "check_stage", "check_question_attachment_count",
                    "check_next_pdf_selection", "check_instructions", "check_same_material"):
            new_request.pop(key, None)
        if parent:
            new_request["origin_id"] = parent["task_id"]
        if not parent:
            note += self._role_summary(roles)
        await self._start_collection(context, tid, request=new_request, note=note)

    @staticmethod
    def _role_summary(roles):
        def numbers(kinds):
            return [str(index) for index, role in enumerate(roles, 1) if role in kinds]
        question = numbers(("question", "both"))
        draft = numbers(("draft", "both"))
        parts = []
        if question:
            parts.append("题目第 " + "、".join(question) + " 份")
        if draft:
            parts.append("草稿第 " + "、".join(draft) + " 份")
        return "材料角色：" + "，".join(parts) + "。"

    async def _auto_check_roles(self, context, task, items):
        tid = task["task_id"]
        images, positions = [], []
        for index, item in enumerate(items):
            if _is_text_file(item):
                continue
            try:
                data = read_cached_attachment(self.store, task, item)
            except (InputCacheError, ValueError) as exc:
                await self._text(context, f"{tid} 第 {index + 1} 份材料校验失败：{exc}。请重新 /补图。")
                return None, ""
            if data is None:
                await self._text(context, f"{tid} 第 {index + 1} 份材料没有本地缓存，请重新 /补图。")
                return None, ""
            try:
                if _is_pdf(item):
                    selection = item.get("pdf_selection") or ""
                    if not selection:
                        raise PDFError("PDF 未指定页码")
                    directory = self.store.task_dir(tid) / f"role-{index + 1}"
                    try:
                        async with aclosing(render_input_pdf_batches_async(
                                data, directory, selection)) as batches:
                            async for pages, total, numbers, selected in batches:
                                if not selected or not pages:
                                    raise PDFError("PDF 没有读到指定页面")
                                images.append((pages[0], "image/png"))
                                positions.append(index)
                                break
                    finally:
                        shutil.rmtree(directory, ignore_errors=True)
                else:
                    image, mime = normalize_image(data)
                    images.append((image, mime))
                    positions.append(index)
            except (PDFError, RuntimeError) as exc:
                await self._text(context, f"{tid} 第 {index + 1} 份 PDF 无法读取：{exc}")
                return None, ""
            except Exception:
                await self._text(context, f"{tid} 第 {index + 1} 份材料无法识别，请重新发送。")
                return None, ""
        if not images:
            await self._text(context, f"{tid} 里没有可识别的题目图片，请先 /补图 发送题目图。")
            return None, ""

        recognized = {}
        for start in range(0, len(images), 4):
            batch = images[start:start + 4]
            batch_positions = positions[start:start + 4]
            try:
                result = await self.ai.classify_check_materials(batch)
            except AIError as exc:
                await self._text(
                    context,
                    f"{tid} 自动识别材料失败 [{exc.code}]；"
                    f"请用 /查错 {tid} 1题 2-4稿 指定角色。")
                return None, ""
            except Exception:
                await self._text(
                    context,
                    f"{tid} 自动识别材料失败；请用 /查错 {tid} 1题 2-4稿 指定角色。")
                return None, ""
            for position, role in zip(batch_positions, result["roles"]):
                recognized[position] = role

        unknown = [str(index + 1) for index in range(len(items))
                   if recognized.get(index) == "unknown"]
        if unknown:
            await self._text(
                context,
                f"{tid} 无法判断第 {'、'.join(unknown)} 份材料的角色；"
                f"请用 /查错 {tid} 1题 2-4稿 指定。")
            return None, ""
        roles = ["draft"] * len(items)
        for position, role in recognized.items():
            roles[position] = role
        if not any(role in ("question", "both") for role in roles):
            await self._text(context, f"{tid} 没有识别到题目材料；请用 /查错 {tid} 1题 2-4稿 指定。")
            return None, ""
        if not any(role in ("draft", "both") for role in roles):
            await self._text(
                context,
                f"{tid} 没有识别到草稿材料；如果题目和作答在同一张图，请用 /查错 {tid} 1同页 指定。")
            return None, ""
        return roles, "已自动识别材料角色。"

    async def _start_collection(self, context, tid, *, request=None, note=""):
        async with self.collection_lock:
            if self.queue.full():
                await self._text(context, f"队列已满，{tid} 的材料仍保留，请稍后再发 /解题 {tid} 或 /查错 {tid}。")
                return
            current = self.store.get(tid, context.owner)
            if current is None:
                await self._text(context, "未找到你在本会话中的这个题号。")
                return
            if current["state"] != "collecting":
                await self._text(context, f"{tid} 已开始或不在收集状态，请按题号查询进度。")
                return
            snapshot = current["request"]
            effective = request if request is not None else snapshot
            try:
                self._pdf_page_budget(effective)
            except (PDFError, ValueError) as exc:
                await self._text(context, str(exc))
                return
            try:
                task = self.store.start_collection(
                    tid, context.owner, max_active=self.settings.max_queue,
                    max_user_active=self.settings.max_user_tasks, request=request)
            except (ValueError, QueueFull) as exc:
                await self._text(context, str(exc))
                return
            self.contexts[tid] = context
            receipt = self._registration_receipt(tid, task["request"].get("action"), task["request"])
            await self._text(context, (note + "\n" + receipt) if note else receipt)
            try:
                self.queue.put_nowait(tid)
            except asyncio.QueueFull:
                self.contexts.pop(tid, None)
                self.store.update(tid, state="collecting")
                if request is not None:
                    try:
                        self.store.update_collection_request(tid, context.owner, snapshot)
                    except ValueError:
                        pass
                await self._text(context, f"队列暂满，材料已保留，请稍后再发 /解题 {tid} 或 /查错 {tid}。")

    async def _submit(self, context, request):
        try:
            self._pdf_page_budget(request)
        except (PDFError, ValueError) as exc:
            await self._text(context, str(exc))
            return
        self.store.cleanup(self.settings.result_ttl)
        if self.store.disk_bytes() >= self.settings.disk_limit_mb * 1024**2:
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
        await self._text(context, self._registration_receipt(tid, request.get("action"), request))
        try:
            self.queue.put_nowait(tid)
        except asyncio.QueueFull:
            self.contexts.pop(tid, None)
            self.store.update(tid, state="failed", error="队列正在释放取消的任务，请稍后按题号重试。")
            await self._text(context, describe(self.store.get(tid)))

    async def _cancel_task(self, context, task):
        tid = task["task_id"]
        targets, seen = [], set()
        for item in [task, *self.store.active_dependents(context.owner, tid)]:
            if item["task_id"] not in seen:
                seen.add(item["task_id"])
                targets.append(item)
        cancelled = []
        for item in targets:
            current = self.store.get(item["task_id"], context.owner)
            if current is None:
                continue
            if current["state"] == "collecting":
                async with self.collection_lock:
                    latest = self.store.get(item["task_id"], context.owner)
                    if not latest or latest["state"] != "collecting":
                        continue
                    self.store.update(item["task_id"], state="cancelled", error="用户取消材料收集")
                cancelled.append(item["task_id"])
            elif current["state"] in ACTIVE:
                self.store.update(item["task_id"], state="cancelled", error="用户取消")
                if not hasattr(self, "cancel_requested"):
                    self.cancel_requested = set()
                self.cancel_requested.add(item["task_id"])
                if self.running_id == item["task_id"] and self.running_task:
                    self.running_task.cancel()
                cancelled.append(item["task_id"])
        if not cancelled:
            await self._text(context, "该题当前没有运行中的任务。")
            return
        await self._text(context, "已取消：" + "、".join(cancelled) + "。")

    async def _retry_task(self, context, task):
        tid = task["task_id"]
        if task["state"] == "collecting":
            await self._text(context, f"这道题尚未开始解答，请发 /解题 {tid} 或 /查错 {tid}，无需重试。")
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
        if row is None:
            log.warning("task_failure_row_missing task=%s", tid)
            return
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
                    if tid in getattr(self, "cancel_requested", set()):
                        # A /取消 arrived between the queue check and task creation.
                        self.running_task.cancel()
                    try:
                        await asyncio.wait_for(self.running_task, timeout=self.settings.job_timeout)
                    except asyncio.CancelledError:
                        cancel_requested = getattr(self, "cancel_requested", set())
                        row = self.store.get(tid)
                        user_cancelled = tid in cancel_requested or (
                            row is not None and row["state"] == "cancelled")
                        if asyncio.current_task().cancelling() or not user_cancelled:
                            raise
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
                task = self.store.get(tid)
                if context and task and (context.passive_available() or self.settings.active_delivery):
                    try:
                        await self._deliver(task, context)
                    except Exception as exc:
                        self.store.update(tid, delivery="awaiting_query")
                        log.warning("delivery_interrupted task=%s type=%s", tid, type(exc).__name__)
                else:
                    if task:
                        self.store.update(tid, delivery="awaiting_query")
            finally:
                self.contexts.pop(tid, None)
                getattr(self, "cancel_requested", set()).discard(tid)
                self.queue.task_done()
