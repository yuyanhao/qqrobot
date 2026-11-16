"""QQ replies, result delivery and on-demand PDF rendering."""
from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
import time

from qqmedia import QQMediaError
from render_service import RenderError, render_favorites_async, render_preview_async
from solver import study_problem
from task_store import describe

log = logging.getLogger("qq-math")


@dataclass
class ReplyContext:
    kind: str
    target: str
    owner: str
    message_id: str
    received: float
    seq: int = 0

    @property
    def reply_limit(self):
        return 5 if self.kind == "group" else 4

    def passive_available(self):
        return (time.time() - self.received < (290 if self.kind == "group" else 3585)
                and self.seq < self.reply_limit)


class TaskDelivery:
    """Reply operations used by TaskBot and the task worker."""

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

    async def _command_choices(self, context, text, choices):
        send_choices = getattr(self.media, "send_command_choices", None)
        if send_choices:
            try:
                await self._send(context, send_choices, text, choices)
                return
            except QQMediaError as exc:
                log.info("command_buttons_unavailable category=%s status=%s", exc.category, exc.status)
        await self._text(context, text)

    async def _send_favorite_pdf(self, context, favorites, title):
        entries = []
        for favorite in favorites:
            tid = favorite["task_id"]
            task = self.store.get(tid, context.owner)
            solution = task["result"].get("solution") if task and task["state"] == "completed" else None
            if (not favorite["answer"] or (solution and
                    study_problem(task["problem"], task["request"]) != favorite["problem"])):
                solution = None
            doc = dict(solution or {"answer_latex": favorite["answer"] or "答案尚未生成。"})
            doc.update(title=tid, problem_latex=favorite["problem"] or "题干尚未读取完成。")
            notes = ["错因：" + favorite["note"]] if favorite["note"] else []
            if favorite["question_ocr"].get("needs_confirmation"):
                notes.append("题干有识图疑点，请在 QQ 中用题干指令核对。")
                notes.extend(favorite["question_ocr"].get("notes", []))
            if notes:
                doc["check_latex"] = "\n\n".join([*notes, doc.get("check_latex", "")]).rstrip()
            entries.append({"doc": doc, "plain": solution is None})

        if self.resource_lock.locked():
            await self._text(context, "当前正在处理题目，请稍后重新导出收藏。")
            return
        try:
            with TemporaryDirectory(prefix="favorites-", dir=self.store.root) as directory:
                async with self.resource_lock:
                    result = await render_favorites_async(title, entries, Path(directory))
                path = Path(directory) / f"{title}.pdf"
                Path(result["pdf_path"]).replace(path)
                info = await self.media.upload_file_path(context.kind, context.target, path)
                await self._send(context, self.media.send_file, info)
                if result["warnings"] and context.passive_available():
                    await self._text(context, "\n".join(result["warnings"]))
        except (RenderError, OSError, QQMediaError) as exc:
            log.warning("favorite_export_failed type=%s", type(exc).__name__)
            await self._text(context, "收藏 PDF 本次未能发送，请稍后重试。")

    async def _deliver(self, task, context, mode="结果", page=1):
        result = task["result"]
        if task["state"] not in ("completed", "needs_input") or not result:
            await self._text(context, describe(task))
            return
        if result.get("study_text"):
            await self._deliver_study(task, context, mode, page)
        else:
            with self.store.retain_result(task["task_id"]):
                await self._deliver_media(task, context, mode, page)

    async def _deliver_study(self, task, context, mode, page):
        tid, result = task["task_id"], task["result"]
        if mode in ("图片", "PDF", "文字稿"):
            await self._text(context, f"{tid} 是学习反馈，发送 {tid} 领取检查反馈。")
            return
        action = task["request"]["action"]
        title = f"提示 {task['request'].get('hint_number', 1)}" if action == "提示" else "查错"
        reply = f"{tid}｜{title}\n{result['study_text']}"
        if result.get("answer_status") == "uncertain":
            reply += "\n\n这条反馈仍有疑点，请结合草稿核对。"
        if task["state"] == "needs_input" and action == "查错":
            reply += f"\n\n补齐后：/查错 {tid} 草稿，也可同条附图。"
        delivered = await self._text(context, reply)
        self.store.update(tid, delivery="delivered" if delivered else "awaiting_query")

    async def _deliver_saved_text(self, task, context, page):
        tid, result = task["task_id"], task["result"]
        path = Path(result["text_path"]) if result.get("text_path") else None
        try:
            if not path or path.resolve().parent != self.store.task_dir(tid):
                return False
            body = path.read_text(encoding="utf-8").strip()
        except (OSError, ValueError):
            return False
        if not body:
            return False

        size = 1400
        total = (len(body) + size - 1) // size
        if not 1 <= page <= total:
            await self._text(context, f"{tid} 的文字稿共 {total} 段，没有第 {page} 段。")
            return True
        verification = result.get("verification") or "未记录"
        status = f"复核：{verification[:180]}"
        if result.get("answer_status") == "uncertain":
            status += "\n这份解答仍有疑点。"
        reply = f"{tid}｜文字稿 {page}/{total}\n{status}\n\n{body[(page - 1) * size:page * size]}"
        if page < total:
            reply += f"\n\n下一段：/结果 {tid} 第{page + 1}段"
        delivered = await self._text(context, reply)
        self.store.update(tid, delivery="delivered" if delivered else "awaiting_query")
        return True

    async def _deliver_media(self, task, context, mode, page):
        tid, result = task["task_id"], task["result"]
        if not context.passive_available() and not self.settings.active_delivery:
            self.store.update(tid, delivery="awaiting_query")
            return
        try:
            if mode == "文字稿":
                if not await self._deliver_saved_text(task, context, page):
                    await self._text(context, f"{tid} 当前没有可用文字稿。")
                return
            path = Path(result["pdf_path"]) if result.get("pdf_path") else None
            if not path or not path.is_file():
                if mode == "结果" and await self._deliver_saved_text(task, context, 1):
                    return
                self.store.update(tid, delivery="awaiting_query")
                reply = describe(task) + ("\n" + result["notes"] if result.get("notes") else "")
                reply += "\n当前没有可用 PDF。"
                if mode != "结果" and result.get("text_path"):
                    reply += f"可发 /结果 {tid} 领取文字稿。"
                await self._text(context, reply)
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
            room = context.reply_limit - context.seq if context.passive_available() else 0
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
            await self._text(context, f"结果已保存，但此次发送未成功。请重新发送 {tid}，或用 /PDF {tid}、/图片 {tid} 1 领取。")
