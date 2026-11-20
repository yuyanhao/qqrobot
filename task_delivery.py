"""QQ replies, result delivery and on-demand PDF rendering."""
from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import time

from qqmedia import QQMediaError
from render_service import render_preview_async
from task_store import describe, short_verification

log = logging.getLogger("qq-math")
TEXT_REPLY_LIMIT = 1800


def _split_text_reply(text, max_parts):
    remaining = str(text or "")
    parts = []
    while remaining and len(parts) < max_parts:
        if len(remaining) <= TEXT_REPLY_LIMIT:
            parts.append(remaining)
            remaining = ""
            break
        boundary = remaining.rfind("\n", 0, TEXT_REPLY_LIMIT + 1)
        if boundary < TEXT_REPLY_LIMIT // 2:
            boundary = TEXT_REPLY_LIMIT
        parts.append(remaining[:boundary].rstrip())
        remaining = remaining[boundary:].lstrip()
    truncated = bool(remaining)
    if truncated and parts:
        marker = "\n… 本次未完整发送，请缩短请求或分批提交材料。"
        parts[-1] = parts[-1][:TEXT_REPLY_LIMIT - len(marker)] + marker
    return parts or [""], truncated


def mention_tag(context):
    """Markdown @ tag for the asking member; group chats render it, C2C does not."""
    if getattr(context, "kind", "") != "group":
        return ""
    member = str(getattr(context, "owner", "")).rsplit(":", 1)[-1]
    return f'<qqbot-at-user id="{member}" />' if member else ""


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
            raise QQMediaError("回复次数用尽，请发题号重取")

    async def _text(self, context, text, *, max_parts=None):
        allowed_parts = max(1, context.reply_limit - context.seq)
        if self.settings.active_delivery:
            allowed_parts = max(allowed_parts, context.reply_limit)
        if max_parts is not None:
            allowed_parts = min(allowed_parts, max(1, int(max_parts)))
        parts, truncated = _split_text_reply(text, allowed_parts)
        if truncated:
            log.warning("text_reply_truncated chars=%s parts=%s", len(str(text or "")), len(parts))
        for index, part in enumerate(parts, 1):
            try:
                await self._send(context, self.media.send_text, part)
            except Exception as exc:
                log.warning("text_delivery_failed part=%s/%s type=%s", index, len(parts), type(exc).__name__)
                return False
        return True

    async def _command_choices(self, context, text, choices, *, per_row=1, fallback=None,
                               inline_mention=False):
        plain = text
        mention = mention_tag(context)
        if mention:
            text = mention if text == "\u200b" else (f"{mention} {text}" if inline_mention
                                                      else f"{mention}\n{text}")
        send_choices = getattr(self.media, "send_command_choices", None)
        if send_choices:
            async def layout(kind, target, content, buttons, msg_id=None, msg_seq=None):
                await send_choices(kind, target, content, buttons, msg_id, msg_seq, per_row)

            try:
                await self._send(context, layout, text, choices)
                return
            except QQMediaError as exc:
                log.info("command_buttons_unavailable category=%s status=%s code=%s",
                         exc.category, exc.status, exc.code)
        await self._text(context, fallback or plain)

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
        if mode in ("PDF", "文字稿"):
            await self._text(context, f"{tid} 是学习反馈，发 {tid} 领取。")
            return
        action = task["request"]["action"]
        title = f"提示 {task['request'].get('hint_number', 1)}" if action == "提示" else "查错"
        reply = f"{tid} · {title}\n{result['study_text']}"
        if result.get("answer_status") == "uncertain":
            reply += "\n\n· 有疑点，请结合草稿核对"
        if task["state"] == "needs_input" and action == "查错":
            reply += f"\n\n补齐后：/查错 {tid} 草稿"
        delivered = await self._text(context, reply)
        self.store.update(tid, delivery="delivered" if delivered else "awaiting_query")

    async def _deliver_saved_text(self, task, context, page=None):
        """page=None sends the whole draft; an explicit page sends one segment."""
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
        if page is None:
            numbers = list(range(1, total + 1))
        else:
            if not 1 <= page <= total:
                await self._text(context, f"{tid} 文字稿共 {total} 段，没有第 {page} 段。")
                return True
            numbers = [page]
        status = short_verification(result.get("verification")) or "未复核"
        delivered, sent = True, 0
        for index, number in enumerate(numbers):
            header = (f"{tid} · 文字稿 {number}/{total} · {status}" if total > 1
                      else f"{tid} · 文字稿 · {status}")
            reply = header + "\n\n" + body[(number - 1) * size:number * size]
            if page is not None and number < total:
                reply += f"\n\n下一段：/结果 {tid} 第{number + 1}段"
            if not await self._text(context, reply):
                delivered = False
                break
            sent = index + 1
        remaining = numbers[sent:] if page is None and not delivered else []
        if remaining:
            await self._text(context, f"还有 {len(remaining)} 段：/结果 {tid} 第{remaining[0]}段")
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
                    await self._text(context, f"{tid} 暂无文字稿。")
                return
            path = Path(result["pdf_path"]) if result.get("pdf_path") else None
            if not path or not path.is_file():
                if mode == "结果" and await self._deliver_saved_text(task, context):
                    return
                self.store.update(tid, delivery="awaiting_query")
                reply = describe(task) + ("\n" + result["notes"] if result.get("notes") else "")
                reply += "\n暂无 PDF。"
                if mode != "结果" and result.get("text_path"):
                    reply += f"发 /结果 {tid} 取文字稿。"
                await self._text(context, reply)
                return

            total = result.get("total_pages")
            if mode != "PDF" and (not isinstance(total, int) or total < 1):
                # Older saved results may have a PDF but no cached page count.
                if self.resource_lock.locked():
                    self.store.update(tid, delivery="awaiting_query")
                    await self._text(context, f"正忙，稍后发 /PDF {tid}。")
                    return
                async with self.resource_lock:
                    batch = await render_preview_async(path, self.store.task_dir(tid), 1, 3)
                result.update(total_pages=batch["total_pages"], preview_paths=batch["preview_paths"])
                self.store.update(tid, result=result)
                total = result["total_pages"]

            image_mode = mode == "结果" and total <= 3
            existing = []
            if result.get("notes"):
                log.info("result_notes task=%s notes=%s", tid, result["notes"][:200])
            if image_mode:
                if mode == "结果": page = 1
                if not 1 <= page <= total:
                    await self._text(context, f"{total} 页，没有第 {page} 页。")
                    return
                count = total
                numbers = list(range(page, min(page + count, total + 1)))
                existing = [self.store.task_dir(tid) / f"preview-{number:03d}.png" for number in numbers]
                if not all(p.is_file() for p in existing):
                    if self.resource_lock.locked():
                        self.store.update(tid, delivery="awaiting_query")
                        await self._text(context, f"正忙，稍后发 /PDF {tid}。")
                        return
                    async with self.resource_lock:
                        batch = await render_preview_async(path, self.store.task_dir(tid), page, count)
                    existing = [Path(p) for p in batch["preview_paths"]]
                    if len(existing) != len(numbers):
                        raise QQMediaError("预览不完整，请重试")

            required = len(existing) if image_mode else 1
            room = context.reply_limit - context.seq if context.passive_available() else 0
            if not self.settings.active_delivery and room < required:
                self.store.update(tid, delivery="awaiting_query")
                await self._text(context, f"{tid} 已保存，发 {tid} 取回。")
                return
            # The answer files are the reply: no status line before them.
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
            await self._text(context, f"发送失败，结果已保存；发 {tid} 重取。")
