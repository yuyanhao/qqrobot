"""Route QQ text commands and handle question, study and favorite operations."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import time
from urllib.parse import urlparse

from aiohttp import ClientError

from book_library import (BOOK_ID, BOOK_UPLOAD_DISK_RESERVE_MB, MAX_BOOK_LIBRARY_MB,
                          MAX_BOOK_PDF_MB, BookLibrary)
from file_tools import DownloadError, download_file
from message_input import _is_pdf
from pdf_tools import (MAX_INPUT_PAGES, MAX_INPUT_PDF_MB, PDFError,
                       pdf_pages_from_text, split_pdf_selection)
from solver import solution_text, study_problem
from task_store import ACTIVE, STATES, TASK_RE, confirmation_key, describe

QUESTION_REFERENCE = re.compile(r"#?(" + TASK_RE + r")(?:\s+([\s\S]+))?", re.I)
FAVORITES_PER_PAGE = 8
PENDING_BOOK_UPLOAD_TTL_SECONDS = 15 * 60
BOOK_UPLOAD_TIMEOUT_SECONDS = 30 * 60
TASK_ARGUMENTS = {
    "进度": "", "结果": "", "图片": " [起始页]", "取消": "", "重试": "",
    "追问": " 具体问题", "补充": " 补充条件", "补图": "（同条附图）", "开始": "", "解题": "", "PDF": "",
}
TASK_COMMAND = re.compile(
    r"/?(?:(" + "|".join(TASK_ARGUMENTS) + r")\s+)?#?(" + TASK_RE + r")(?:\s+([\s\S]+))?", re.I
)
RESULT_PART = re.compile(r"第\s*(\d{1,8})\s*段")
QUESTION_ID_COMMANDS = {
    **TASK_ARGUMENTS,
    "查错": " 草稿", "题干": " [段号]", "确认题干": " [更正]", "取消收藏": "",
}
PICKER_STATES = {
    "补图": ("collecting",),
    "开始": ("collecting",),
    "解题": ("collecting",),
    "取消": ("collecting", *ACTIVE),
    "确认题干": ("needs_input",),
}

HELP = ("发文字或图片即可解题，机器人回复题号。\n"
        "题号：查进度、领结果；/进度 题号；/PDF 题号；/图片 题号 页码；/结果 题号 第2段\n"
        "/追问 题号 问题；/补充 题号 条件；/重试 题号；/取消 题号\n"
        "/收集 [说明] → /补图 收集号 → /开始 收集号（或 /解题）\n"
        "/查错：收集材料后发 /查错 收集号；或 /查错 题号 [同页] 附草稿\n"
        "/题干 题号；/确认题干 题号 [更正]\n"
        "/收藏 题号；/复习；/错题本；/题目；/最近\n"
        "/书库；/引用 B0001 第3页 [要求]\n"
        "/帮助：本说明\n"
        f"\nPDF 须写明页码，每份和每题最多 {MAX_INPUT_PAGES} 页、{MAX_INPUT_PDF_MB} MB；"
        "每条最多 4 个附件，收集题累计最多 12 个。\n"
        "群聊每条操作需 @ 机器人；支持全角斜杠。")


class TaskCommands:
    """Command routing for the bot's text replies, queue and stored results."""

    def _schedule_pending_book_upload_expiry(self, target, path, delay):
        asyncio.get_running_loop().call_later(
            delay, lambda: asyncio.create_task(self._expire_pending_book_upload(target, path))
        )

    async def _expire_pending_book_upload(self, target, path):
        async with self.book_import_lock:
            pending = self.pending_book_uploads.get(target)
            if not pending or pending[1] != path:
                return
            remaining = pending[0] + PENDING_BOOK_UPLOAD_TTL_SECONDS - time.monotonic()
            if remaining > 0:
                self._schedule_pending_book_upload_expiry(target, path, remaining)
                return
            self.pending_book_uploads.pop(target, None)
            with suppress(OSError):
                path.unlink(missing_ok=True)

    async def _stage_book_upload(self, context, attachments):
        if context.kind != "c2c" or context.target != getattr(self, "book_admin_openid", ""):
            return False
        pdfs = [item for item in attachments if _is_pdf(item)]
        if len(pdfs) > 1:
            await self._text(context, "一次只能暂存一份 PDF。")
            return True
        if pdfs:
            if len(attachments) > 1:
                await self._text(context, "暂存书库 PDF 时只发这一份文件。")
                return True
            attachment = pdfs[0]
        elif len(attachments) == 1:
            attachment = attachments[0]
            if not self._is_ambiguous_book_attachment(attachment):
                return False
        else:
            return False

        url = attachment.get("url", "")
        if url.startswith("//"):
            url = "https:" + url
        elif url and "://" not in url:
            url = "https://" + url
        if not url:
            await self._text(context, "文件还没上传完，请稍后重发。")
            return True

        staged = None
        try:
            async with self.book_import_lock:
                with tempfile.NamedTemporaryFile(
                    prefix=".book-upload-", suffix=".pdf", dir=self.store.root, delete=False
                ) as output:
                    staged = Path(output.name)
                    await download_file(url, max_mb=self._book_upload_limit_mb(),
                                        timeout=BOOK_UPLOAD_TIMEOUT_SECONDS, output=output)
                with staged.open("rb") as uploaded:
                    if b"%PDF-" not in uploaded.read(1024):
                        staged.unlink(missing_ok=True)
                        return False
                previous = self.pending_book_uploads.get(context.target)
                self.pending_book_uploads[context.target] = (time.monotonic(), staged)
                self._schedule_pending_book_upload_expiry(
                    context.target, staged, PENDING_BOOK_UPLOAD_TTL_SECONDS
                )
                if previous:
                    with suppress(OSError):
                        previous[1].unlink(missing_ok=True)
        except (ClientError, DownloadError, OSError):
            if staged:
                with suppress(OSError):
                    staged.unlink(missing_ok=True)
            await self._text(context, "暂存失败，请重发文件。")
            return True
        except BaseException:
            if staged:
                with suppress(OSError):
                    staged.unlink(missing_ok=True)
            raise
        await self._text(context, "已暂存（15 分钟内有效）。发 /入库 书名 完成入库。")
        return True

    @staticmethod
    def _is_ambiguous_book_attachment(attachment):
        content_type = str(attachment.get("content_type") or "").lower().strip()
        name = str(attachment.get("name") or "").strip()
        url_path = urlparse(str(attachment.get("url") or "")).path
        if content_type.startswith(("image/", "text/")):
            return False
        known_suffixes = {
            ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff",
            ".txt", ".md", ".markdown", ".tex",
        }
        suffixes = {Path(value).suffix.lower() for value in (name, url_path) if value}
        if any(suffix in known_suffixes for suffix in suffixes):
            return False
        return content_type in ("", "application/octet-stream", "binary/octet-stream", "application/x-download")

    def _book_upload_limit_mb(self):
        free_mb = shutil.disk_usage(self.store.root).free // 1024**2
        library = BookLibrary(self.store.root)
        remaining_library_mb = (MAX_BOOK_LIBRARY_MB * 1024**2 - library.stored_bytes()) // 1024**2
        max_mb = min(MAX_BOOK_PDF_MB, remaining_library_mb, free_mb - BOOK_UPLOAD_DISK_RESERVE_MB)
        if max_mb < 1:
            raise DownloadError(
                f"书库容量已满或磁盘可用空间不足；每份 PDF 最多 {MAX_BOOK_PDF_MB} MB，"
                f"书库总量最多 {MAX_BOOK_LIBRARY_MB} MB，磁盘需保留 {BOOK_UPLOAD_DISK_RESERVE_MB} MB。")
        return int(max_mb)

    async def _dispatch_command(self, context, text, quoted, attachments, *, quoted_urls=()):
        if text in ("/帮助", "帮助", "/help", "你好", "您好"):
            await self._text(context, HELP)
            return
        if text in ("/题目", "题目", "我的题目"):
            recent = self.store.list_recent(context.owner)
            await self._text(context, "\n\n".join(describe(t) for t in recent) or "还没有题目。")
            return
        parts = text.split(maxsplit=1) if text.startswith("/") else []
        command = parts[0][1:] if parts else ""
        args = parts[1].strip() if len(parts) == 2 else ""
        if text == "/取消":
            await self._cancel_latest(context)
            return
        action = "PDF" if command.casefold() == "pdf" else command
        if action == "最近" and not args:
            await self._offer_question_choices(context, action)
            return
        if (context.kind == "group" and not args and not quoted and not attachments
                and action in QUESTION_ID_COMMANDS and action != "查错"):
            if await self._offer_question_choices(context, action):
                return
        if command == "查错" and args.casefold() == "最近":
            await self._offer_check_question_choices(context)
            return
        if command == "查错":
            await self._check_command(context, args, quoted, attachments, quoted_urls)
            return
        if command in ("题干", "确认题干"):
            await self._question_command(context, command, args)
            return
        if command in ("书库", "引用", "入库", "移除书籍"):
            await self._book_command(context, command, args, attachments)
            return
        if command in ("收藏", "复习", "取消收藏", "错题本"):
            await self._favorite_command(context, command, args)
            return
        collection = re.fullmatch(r"/?收集(?:\s+([\s\S]+))?", text)
        if collection:
            collection_text = (collection[1] or "").strip()
            parent = QUESTION_REFERENCE.fullmatch(collection_text)
            parent_id = parent[1].upper() if parent else ""
            if parent:
                collection_text = (parent[2] or "").strip()
            await self._create_collection(context, collection_text, quoted, attachments, parent_id=parent_id)
            return
        match = TASK_COMMAND.fullmatch(text)
        if match:
            action, tid, tail = match[1] or "结果", match[2].upper(), (match[3] or "").strip()
            if action.casefold() == "pdf":
                action = "PDF"
            if match[1] == "结果" and not (attachments or quoted) and (part := RESULT_PART.fullmatch(tail)):
                action, tail = "文字稿", part[1]
            await self._task_command(context, action, tid, tail, quoted, attachments)
            return
        if action in TASK_ARGUMENTS:
            await self._text(context, f"用法：/{action} 题号{TASK_ARGUMENTS[action]}")
            return
        if text.startswith(("/搜", "/问", "/读", "/文件")):
            await self._text(context, "只支持数学物理题，直接发题即可。")
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

    async def _offer_check_question_choices(self, context):
        rows = self.store.recent_question_choices(
            context.owner, ttl_days=self.settings.result_ttl, ready_to_check=True)
        beijing = timezone(timedelta(hours=8))
        candidates = [(row["task_id"],
                       datetime.fromtimestamp(row["created"], beijing).strftime("%m-%d %H:%M")
                       + " · " + STATES.get(row["state"], row["state"])) for row in rows]
        if not candidates:
            await self._text(context, "没有可继续查错的已保存题目；先发 /收集 收集题目和草稿。")
            return
        lines = ["选要查错的题，之后补草稿。",
                 "点题号填入指令。"]
        choices = []
        for index, (tid, status) in enumerate(candidates, 1):
            command = f"/收集 {tid}"
            lines.extend((f"{index}. {status}", command))
            choices.append((tid, command))
        await self._command_choices(context, "\n".join(lines), choices)

    async def _offer_question_choices(self, context, action):
        if action == "取消收藏":
            rows = self.store.list_favorites(context.owner, 3)
            candidates = [(row["task_id"], "已收藏") for row in rows]
        else:
            rows = self.store.recent_question_choices(
                context.owner, ttl_days=self.settings.result_ttl,
                states=PICKER_STATES.get(action, ()), needs_confirmation=action == "确认题干",
                ready_to_check=action == "查错",
            )
            beijing = timezone(timedelta(hours=8))
            candidates = [(row["task_id"], datetime.fromtimestamp(row["created"], beijing).strftime("%m-%d %H:%M")
                           + " · " + STATES.get(row["state"], row["state"])) for row in rows]
        if not candidates:
            if action == "最近":
                await self._text(context, "还没有近期题目。")
            elif action == "查错":
                await self._text(context, "没有可继续查错的题目。")
            return False

        lines = (["最近题目，点题号填入输入框。"] if action == "最近"
                 else [f"用法：/{action} 题号{QUESTION_ID_COMMANDS[action]}"])
        choices = []
        for index, (tid, status) in enumerate(candidates, 1):
            command = tid if action == "最近" else f"/{action} {tid}"
            lines.extend((f"{index}. {status}", command))
            choices.append((tid, command))
        await self._command_choices(context, "\n".join(lines), choices)
        return True

    async def _task_command(self, context, action, tid, tail, quoted, attachments):
        if await self._show_confirmed_question(context, tid):
            return
        task = self.store.get(tid, context.owner)
        if task is None:
            await self._text(context, "未找到该题号。")
            return
        if action == "补图" or (action == "补充" and task["state"] == "collecting"):
            if task["state"] != "collecting":
                await self._text(context, f"/补图 只用于收集中题目；单次补充发 /补充 {tid}。")
                return
            await self._append_collection(context, tid, tail, quoted, attachments)
        elif action in ("开始", "解题"):
            if tail or attachments or quoted:
                await self._text(context, f"先 /补图 {tid} 追加材料，再发 /{action} {tid}。")
            else:
                await self._start_collection(context, tid)
        elif action in ("追问", "补充") or (action == "结果" and tail):
            if task["state"] == "collecting":
                await self._text(context, f"还在收集材料：先 /解题 {tid} 或 /查错 {tid}。")
                return
            if not tail and not quoted and not attachments:
                await self._text(context, "请写内容或同条附图；引用图片失效时请重发原图。")
                return
            await self._submit(context, {"text": tail, "parent_id": tid, "quoted": quoted,
                                         "attachments": attachments,
                                         "action": "补充" if action == "补充" else "追问"})
        elif action == "取消":
            await self._cancel_task(context, task)
        elif action == "重试":
            await self._retry_task(context, task)
        elif action == "PDF":
            if tail:
                await self._text(context, f"用法：/PDF {tid}")
            else:
                await self._deliver(task, context, "PDF")
        elif action == "文字稿":
            await self._deliver(task, context, action, int(tail))
        elif action == "图片":
            try:
                page = int(tail or "1")
            except ValueError:
                page = 0
            if page < 1:
                await self._text(context, "页码必须为正整数。")
            else:
                await self._deliver(task, context, action, page)
        elif action == "进度":
            await self._text(context, describe(task))
        else:
            await self._deliver(task, context)

    async def _cancel_latest(self, context):
        for task in self.store.list_recent(context.owner, limit=10):
            if task["state"] == "collecting" or task["state"] in ACTIVE:
                await self._cancel_task(context, task)
                return
        await self._text(context, "没有可取消的任务。")

    def _matching_origin(self, task, owner, problem):
        origin_id = task["request"].get("origin_id") if task else None
        if not origin_id or not problem:
            return None
        origin = self.store.get(origin_id, owner)
        if origin and study_problem(origin["problem"], origin["request"]) == problem:
            return origin
        return None

    async def _show_confirmed_question(self, context, tid):
        successor = self.store.confirmation_successor(tid, context.owner)
        if successor:
            await self._text(context, f"{tid} 已校对，继续用 {successor}。"
                                      f"发 {successor} 查进度。")
        return bool(successor)

    @staticmethod
    def _confirmation_help(tid):
        return f"发 /题干 {tid} 核对，再发 /确认题干 {tid} [更正] 继续。"

    async def _question_command(self, context, action, args):
        reference = QUESTION_REFERENCE.fullmatch(args)
        if not reference:
            await self._text(context, f"用法：/{action} 题号" + (" [段号]" if action == "题干" else " [更正]"))
            return
        tid, tail = reference[1].upper(), (reference[2] or "").strip()
        if (action == "题干" or not tail) and await self._show_confirmed_question(context, tid):
            return
        source = self.store.get(tid, context.owner)
        if source and source["problem"]:
            problem = study_problem(source["problem"], source["request"])
            ocr = source["question_ocr"]
        else:
            favorite = self.store.get_favorite(tid, context.owner)
            if not favorite:
                reply = "未找到该题号。" if not source else f"{tid} 尚无可用题干。"
                await self._text(context, reply)
                return
            problem, ocr = favorite["problem"], favorite["question_ocr"]
        if action == "题干":
            if tail and not re.fullmatch(r"[1-9][0-9]{0,7}", tail):
                await self._text(context, "段号必须为正整数。")
                return
            page = int(tail or "1")
            heading = "原识图备注（已由你确认题干）" if ocr.get("confirmed") else "识图备注"
            notes = "\n".join(ocr.get("notes", []))
            contents = f"{heading}\n{notes}\n\n题干\n{problem}" if notes else problem
            size = 1400
            total = (len(contents) + size - 1) // size
            if page > total:
                await self._text(context, f"题干共 {total} 段，没有第 {page} 段。")
                return
            reply = f"{tid}｜题干 {page}/{total}\n{contents[(page - 1) * size:page * size]}"
            if page < total:
                reply += f"\n\n下一段：/题干 {tid} {page + 1}"
            if ocr.get("needs_confirmation"):
                reply += f"\n\n{self._confirmation_help(tid)}"
            await self._text(context, reply)
            return
        if not ocr.get("needs_confirmation"):
            await self._text(context, "题干无需确认。")
            return
        if tail:
            problem += "\n\n用户核对后的更正（以本段为准）：\n" + tail
        if len(problem) > 24000:
            await self._text(context, "题干太长，请按小问拆分。")
            return
        confirmed = {**ocr, "confirmed": True, "needs_confirmation": False}
        key = confirmation_key(tid, problem, confirmed)
        existing = self.store.get_reusable(context.owner, key)
        if existing:
            await self._deliver(existing, context)
            return
        origin_id = (source["request"].get("origin_id") or tid) if source else tid
        request = {"text": problem, "question_ocr": confirmed, "reuse_key": key, "confirmation_of": tid}
        if self.store.get_favorite(tid, context.owner):
            request["favorite_id"] = tid
        if ocr.get("mode") == "查错" and source:
            draft = source["request"]
            request.update(action="查错", text=draft.get("text", ""), quoted=draft.get("quoted", ""),
                           attachments=draft.get("attachments", []), problem_snapshot=problem, origin_id=origin_id)
        await self._submit(context, request)

    async def _check_command(self, context, args, quoted, attachments, quoted_urls):
        if len(attachments) > 4:
            await self._text(context, "同一条消息最多附 4 个文件或图片。")
            return
        reference = QUESTION_REFERENCE.fullmatch(args)
        if reference:
            tid, tail = reference[1].upper(), (reference[2] or "").strip()
            if await self._show_confirmed_question(context, tid):
                return
            source = self.store.get(tid, context.owner)
            if source and source["state"] == "collecting":
                if quoted:
                    await self._text(context, f"先 /补图 {tid} 追加材料。")
                    return
                await self._start_collection_check(context, source, tail, attachments)
                return
            favorite = self.store.get_favorite(tid, context.owner)
            if source and source["problem"]:
                problem, ocr = study_problem(source["problem"], source["request"]), source["question_ocr"]
            elif favorite and favorite["problem"]:
                problem, ocr = favorite["problem"], favorite["question_ocr"]
            else:
                await self._text(context, f"未找到 {tid} 的题干；可先 /收集 {tid}。")
                return
            if ocr.get("needs_confirmation"):
                await self._text(context, self._confirmation_help(tid))
                return
            if re.match(r"^同页(?:\s+|$)", tail):
                tail = re.sub(r"^同页\s*", "", tail, count=1).strip()
            if not attachments and not quoted and not tail:
                await self._text(context, f"请同条附草稿，或先 /收集 {tid} 再 /查错 收集号。")
                return
            request = {"action": "查错", "text": tail, "quoted": quoted,
                       "attachments": list(attachments), "problem_snapshot": problem,
                       "question_ocr": ocr, "origin_id": tid}
            await self._submit_check(context, request)
            return

        if re.match(r"B\d+", args, re.I):
            try:
                book, tail = BookLibrary(self.store.root).reference(args)
            except (PDFError, ValueError) as exc:
                await self._text(context, str(exc))
                return
            if any(_is_pdf(item) for item in attachments):
                await self._text(context, "草稿含 PDF：先 /收集 B0001 第3页。")
                return
            if not attachments and not quoted and not tail:
                await self._text(context, "请同条附草稿，或先 /收集 B0001 第3页。")
                return
            request = {"action": "查错", "text": tail, "quoted": quoted,
                       "attachments": list(attachments), "question_attachments": [book]}
            await self._submit_check(context, request)
            return

        if not (attachments or quoted):
            await self._text(
                context,
                "用法：/查错 收集号，或 /查错 题号 附草稿。")
            return

        same_material = False
        tail = args
        if re.match(r"^同页(?:\s+|$)", tail):
            same_material = True
            tail = re.sub(r"^同页\s*", "", tail, count=1).strip()

        if quoted or quoted_urls:
            questions = [item for item in attachments if item["url"] in quoted_urls]
            drafts = [item for item in attachments if item["url"] not in quoted_urls]
            if not drafts and not tail and questions:
                drafts = list(questions)
            request = {"action": "查错", "text": tail, "quoted": "",
                       "attachments": drafts, "question_attachments": questions,
                       "problem_snapshot": quoted}
        else:
            pdf_attachments = any(_is_pdf(item) for item in attachments)
            if pdf_attachments:
                try:
                    selection, draft_text = split_pdf_selection(tail)
                    if not selection:
                        raise PDFError(
                            f"PDF 请写明页码（最多 {MAX_INPUT_PAGES} 页），例如 /查错 第3页 草稿。")
                except PDFError as exc:
                    await self._text(context, str(exc))
                    return
            else:
                selection, draft_text = "", tail
            if len(attachments) == 1:
                item = attachments[0]
                if same_material or not draft_text:
                    questions = drafts = [item]
                else:
                    questions, drafts = [item], []
            elif same_material:
                await self._text(context, "同页只接受一份；多份请走 /收集。")
                return
            else:
                questions, drafts = [attachments[0]], list(attachments[1:])
            if pdf_attachments:
                questions = [({**item, "pdf_selection": selection} if _is_pdf(item) else item)
                             for item in questions]
                drafts = [({**item, "pdf_selection": selection} if _is_pdf(item) else item)
                          for item in drafts]
            request = {"action": "查错", "text": draft_text, "quoted": "",
                       "attachments": drafts, "question_attachments": questions}
        await self._submit_check(context, request)

    async def _submit_check(self, context, request):
        attachments = list(request.get("attachments") or [])
        questions = list(request.get("question_attachments") or [])
        pdfs = [item for item in [*questions, *attachments] if _is_pdf(item)]
        if pdfs and any(not item.get("pdf_selection") for item in pdfs):
            try:
                selection, request["text"] = split_pdf_selection(request.get("text") or "")
                if not selection:
                    raise PDFError(
                        f"PDF 请写明页码（最多 {MAX_INPUT_PAGES} 页）。")
            except PDFError as exc:
                await self._text(context, str(exc))
                return
            attachments = [({**item, "pdf_selection": selection} if _is_pdf(item) else item)
                           for item in attachments]
            questions = [({**item, "pdf_selection": selection} if _is_pdf(item) else item)
                         for item in questions]
        request["attachments"] = attachments
        request["question_attachments"] = questions
        if not (request.get("text") or request.get("quoted") or attachments):
            await self._text(context, "请附草稿图或写推导。")
            return
        if len(request.get("text") or "") + len(request.get("quoted") or "") > 24000:
            await self._text(context, "材料超过 2.4 万字符，请按小问拆分。")
            return
        await self._submit(context, request)

    async def _book_command(self, context, action, args, attachments):
        if not hasattr(self, "pending_book_uploads"):
            self.pending_book_uploads = {}
        library = BookLibrary(self.store.root)
        try:
            if action in ("入库", "移除书籍"):
                if (context.kind != "c2c" or context.target != getattr(self, "book_admin_openid", "")
                        or not context.target):
                    await self._text(context, "仅管理员私聊可管理书库。")
                    return
                if action == "移除书籍":
                    async with self.book_import_lock:
                        book = library.remove(args)
                        self._cleanup_retired_books()
                    await self._text(context, f"已移除 {book['id']}。")
                    return
                if not args:
                    await self._text(context, "格式：/入库 书名 [| 简介]。")
                    return
                pdfs = [item for item in attachments if _is_pdf(item)]
                if len(pdfs) > 1:
                    await self._text(context, "一次只能附一份 PDF。")
                    return
                if pdfs and len(attachments) > 1:
                    await self._text(context, "入库只附一份 PDF。")
                    return
                if pdfs:
                    attachment = pdfs[0]
                elif len(attachments) == 1:
                    # QQ may omit the filename or use a generic MIME type for a file card.
                    # Let BookLibrary.add validate the downloaded bytes as a real PDF.
                    attachment = attachments[0]
                elif not attachments:
                    attachment = None
                else:
                    await self._text(context, "无法确定哪份是 PDF，请只附一份。")
                    return
                async with self.book_import_lock:
                    title, _, summary = args.replace("｜", "|", 1).partition("|")
                    pending = self.pending_book_uploads.get(context.target) if attachment is None else None
                    if (pending and (time.monotonic() - pending[0] > PENDING_BOOK_UPLOAD_TTL_SECONDS
                                     or not pending[1].is_file())):
                        self.pending_book_uploads.pop(context.target, None)
                        with suppress(OSError):
                            pending[1].unlink(missing_ok=True)
                        pending = None
                    if attachment is None and pending is None:
                        await self._text(context, "没有待入库 PDF：先私聊发 PDF。")
                        return

                    if pending:
                        source_path = pending[1]
                        registration = asyncio.create_task(
                            asyncio.to_thread(library.add, source_path, title, summary)
                        )
                        try:
                            book = await asyncio.shield(registration)
                        except asyncio.CancelledError:
                            with suppress(Exception):
                                await registration
                            raise
                    else:
                        url = attachment.get("url", "")
                        if url.startswith("//"):
                            url = "https:" + url
                        elif url and "://" not in url:
                            url = "https://" + url
                        with library.staged_pdf() as staged:
                            with staged.open("xb") as output:
                                await download_file(url, max_mb=self._book_upload_limit_mb(),
                                                    timeout=BOOK_UPLOAD_TIMEOUT_SECONDS, output=output)
                            registration = asyncio.create_task(
                                asyncio.to_thread(library.add, staged, title, summary)
                            )
                            try:
                                book = await asyncio.shield(registration)
                            except asyncio.CancelledError:
                                # Keep the staged file alive until the catalog update has finished.
                                with suppress(Exception):
                                    await registration
                                raise
                    completed_pending = self.pending_book_uploads.pop(context.target, None)
                    if completed_pending:
                        with suppress(OSError):
                            completed_pending[1].unlink(missing_ok=True)
                await self._text(context, f"已入库 {book['id']} · {book['pages']} 页。")
                return
            if action == "引用" and re.match(rf"{BOOK_ID}\s+", args, re.I):
                item, tail = library.reference(args)
                if len(attachments) >= 4:
                    await self._text(context, "引用书籍时，还可同条附最多 3 个材料。")
                    return
                request = {"text": "", "extra": tail, "quoted": "",
                           "attachments": [item, *attachments]}
                if any(_is_pdf(extra) for extra in attachments):
                    try:
                        pages = pdf_pages_from_text(tail)
                    except PDFError as exc:
                        await self._text(context, str(exc))
                        return
                    if pages:
                        selection = "第" + "、".join(map(str, sorted(pages))) + "页"
                        request["attachments"] = [
                            {**extra, "pdf_selection": selection} if _is_pdf(extra) else extra
                            for extra in attachments]
                await self._submit(context, request)
                return
            if action == "引用" and re.fullmatch(BOOK_ID, args, re.I):
                book = library.get_by_display_id(args)
                await self._text(context, f"《{book['title']}》{book['pages']} 页：发 /引用 {book['id']} 第3页 [要求]。")
                return
            if args and not re.fullmatch(r"[1-9][0-9]{0,5}", args):
                await self._text(context, "用法：/书库 [页] 或 /引用 B0001 第3页。")
                return
            page = int(args or "1")
            size = 5
            books = library.list(size + 1, (page - 1) * size)
            if not books:
                await self._text(context, "这一页没有书籍。")
                return
            lines, choices = ["共享书库｜第 " + str(page) + " 页"], []
            for book in books[:size]:
                command = f"/引用 {book['id']}"
                lines.append(f"{book['id']} 《{book['title']}》 · {book['pages']} 页")
                if book["summary"]:
                    lines.append(book["summary"])
                lines.append(command)
                choices.append((book["id"], command))
            lines.append("点编号后补页码，如 /引用 B0001 第3页。")
            if len(books) > size:
                lines.append(f"下一页：/书库 {page + 1}")
            await self._command_choices(context, "\n".join(lines), choices)
        except ClientError:
            await self._text(context, "下载中断，请重发 PDF。")
        except (PDFError, ValueError, DownloadError, OSError, sqlite3.Error) as exc:
            await self._text(context, f"书库失败：{exc}")

    async def _favorite_command(self, context, action, args):
        owner = context.owner
        reference = QUESTION_REFERENCE.fullmatch(args)
        if action == "错题本":
            favorites = self.store.favorites_in_order(owner)
            if args or not favorites:
                await self._text(context, "发 /错题本 导出当前会话的收藏。" if args else "当前会话没有收藏。")
            else:
                await self._send_favorite_pdf(context, favorites, "错题本")
            return
        if action in ("收藏", "复习") and not reference:
            if args and not re.fullmatch(r"[1-9][0-9]{0,5}", args):
                await self._text(context, f"请发 /{action} 题号，或 /{action} [列表页]。")
                return
            page = int(args or "1")
            size = 5 if action == "复习" else FAVORITES_PER_PAGE
            rows = self.store.favorites_in_order(owner, limit=size + 1, offset=(page - 1) * size)
            if not rows:
                await self._text(context, "本页没有收藏。")
                return
            lines = [f"收藏 · 第 {page} 页"]
            choices = []
            for row in rows[:size]:
                tid = row["task_id"]
                preview = " ".join(row["problem"][:72].split()) or "题干读取中"
                command = f"/复习 {tid}"
                lines.append(f"{command} · {preview}")
                choices.append((tid, command))
            if page > 1:
                lines.append(f"上一页：/{action} {page - 1}")
            if len(rows) > size:
                lines.append(f"下一页：/{action} {page + 1}")
            if action == "复习":
                await self._command_choices(context, "\n".join(lines), choices)
            else:
                await self._text(context, "\n".join(lines))
            return
        if not reference:
            await self._text(context, f"请在 /{action} 后填写题号。")
            return
        tid, tail = reference[1].upper(), (reference[2] or "").strip()
        if tail and action != "收藏":
            await self._text(context, f"用法：/{action} 题号。")
            return
        if action == "取消收藏":
            changed = self.store.remove_favorite(tid, owner)
            await self._text(context, f"已取消收藏 {tid}。" if changed else "未找到该收藏。")
            return
        favorite = self.store.get_favorite(tid, owner)
        if action == "复习":
            if favorite:
                await self._send_favorite_pdf(context, [favorite], f"复习 {tid}")
            else:
                await self._text(context, "未找到该收藏。")
            return

        task = self.store.get(tid, owner)
        if task and task["state"] not in ("expired", "rejected"):
            problem, ocr = study_problem(task["problem"], task["request"]), task["question_ocr"]
            answer_task = (self._matching_origin(task, owner, problem) or task) if problem else task
            result = answer_task["result"] if answer_task["state"] == "completed" else {}
            doc = result.get("solution") if problem and not ocr.get("needs_confirmation") else None
            answer = solution_text(doc, include_problem=False) if doc else result.get("study_text", "")
        elif favorite:
            problem, answer, ocr = favorite["problem"], favorite["answer"], favorite["question_ocr"]
        else:
            await self._text(context, "未找到本会话中可收藏的题目。")
            return
        try:
            self.store.save_favorite(tid, owner, problem, answer, note=tail or None, question_ocr=ocr)
        except ValueError as exc:
            await self._text(context, str(exc))
            return
        await self._text(context, f"已收藏 {tid}。发 /复习 {tid} 取题。")
