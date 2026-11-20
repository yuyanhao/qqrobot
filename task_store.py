"""Durable, owner-scoped tasks. No message content is stored in logs."""
from __future__ import annotations
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import shutil
import sqlite3
import time

from message_input import _is_pdf
from pdf_tools import PDFError, pdf_selection_page_count

ACTIVE = ("queued", "reading", "planning", "searching", "calculating", "solving", "verifying", "rendering")
TASK_RE = r"Q\d{6}-\d{6,10}"
MAX_COLLECTION_ATTACHMENTS = 12
MAX_COLLECTION_UNITS = 5
MAX_COLLECTION_TEXT = 24000


def collection_units(attachments):
    """Count collected material: one image or one selected PDF page is one unit."""
    units = 0
    for item in attachments or ():
        if item.get("book_id") or _is_pdf(item):
            try:
                units += pdf_selection_page_count(item.get("pdf_selection") or "") or 1
            except PDFError:
                units += 1
        else:
            units += 1
    return units


class QueueFull(ValueError):
    pass


class TaskStore:
    def __init__(self, directory: Path):
        self.root = directory.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.results = self.root / "results"
        self.results.mkdir(exist_ok=True)
        self._retained_results = Counter()
        self._disk_bytes_cache = (0.0, 0.0)
        self.db = sqlite3.connect(self.root / "tasks.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT UNIQUE,
            owner TEXT NOT NULL, message_id TEXT NOT NULL, kind TEXT NOT NULL, target TEXT NOT NULL,
            created REAL NOT NULL, updated REAL NOT NULL, state TEXT NOT NULL,
            request TEXT NOT NULL, problem TEXT NOT NULL DEFAULT '',
            result TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
            delivery TEXT NOT NULL DEFAULT 'pending', question_ocr TEXT NOT NULL DEFAULT '{}',
            UNIQUE(owner,message_id))""")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in self.db.execute("PRAGMA table_info(tasks)")}
            if "question_ocr" not in columns:
                self.db.execute("ALTER TABLE tasks ADD COLUMN question_ocr TEXT NOT NULL DEFAULT '{}'")
            self.db.execute("CREATE INDEX IF NOT EXISTS tasks_owner_state ON tasks(owner, state)")
            self.db.execute("CREATE INDEX IF NOT EXISTS tasks_owner_created ON tasks(owner, created)")
            self.db.execute("CREATE INDEX IF NOT EXISTS tasks_state_created ON tasks(state, created)")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")

    def close(self):
        self.db.close()

    @staticmethod
    def _decode(row):
        if row is None: return None
        task = dict(row)
        task["request"] = json.loads(task["request"])
        task["result"] = json.loads(task["result"])
        task["question_ocr"] = json.loads(task["question_ocr"])
        return task

    def get(self, task_id, owner=None):
        sql, params = "SELECT * FROM tasks WHERE task_id=?", [task_id.upper()]
        if owner is not None:
            sql += " AND owner=?"
            params.append(owner)
        return self._decode(self.db.execute(sql, params).fetchone())

    def list_recent(self, owner, limit=5):
        return [self._decode(row) for row in self.db.execute(
            "SELECT * FROM tasks WHERE owner=? ORDER BY id DESC LIMIT ?", (owner, limit))]

    def recent_question_choices(self, owner, *, ttl_days=7, states=(), ready_to_check=False,
                                limit=3):
        threshold = time.time() - ttl_days * 86400
        active_slots = ",".join("?" for _ in ACTIVE)
        sql = f"""SELECT task_id,state,created FROM tasks WHERE owner=? AND state!='expired'
            AND (created>=? OR state IN ({active_slots}))"""
        params = [owner, threshold, *ACTIVE]
        if states:
            sql += " AND state IN (" + ",".join("?" for _ in states) + ")"
            params.extend(states)
        if ready_to_check:
            sql += " AND problem!=''"
        return self.db.execute(sql + " ORDER BY id DESC LIMIT ?", [*params, int(limit)]).fetchall()

    def collecting(self, owner):
        return self._decode(self.db.execute(
            "SELECT * FROM tasks WHERE owner=? AND state='collecting' ORDER BY id DESC LIMIT 1",
            (owner,)).fetchone())

    def active_dependents(self, owner, task_id):
        """Active or collecting tasks derived from task_id (follow-up, retry, check)."""
        slots = ",".join("?" for _ in ACTIVE)
        rows = self.db.execute(
            f"SELECT * FROM tasks WHERE owner=? AND (state='collecting' OR state IN ({slots}))",
            (owner, *ACTIVE)).fetchall()
        dependents = []
        for row in rows:
            request = json.loads(row["request"])
            if task_id in (request.get("parent_id"), request.get("retry_of"), request.get("origin_id")):
                dependents.append(self._decode(row))
        return dependents

    @staticmethod
    def _collection_materials(request):
        if not isinstance(request, dict):
            raise ValueError("题目材料格式无效")
        text, quoted = str(request.get("text") or ""), str(request.get("quoted") or "")
        if len(text) + len(quoted) > MAX_COLLECTION_TEXT:
            raise ValueError("收集的文字超过 2.4 万字符，请按题目拆分")
        items = request.get("attachments") or []
        if not isinstance(items, (list, tuple)) or any(not isinstance(item, dict) for item in items):
            raise ValueError("附件材料格式无效")
        attachments, hashes, urls = [], set(), set()
        for item in items:
            digest, url = item.get("sha256"), item.get("url")
            selection = item.get("pdf_selection", "")
            if not digest and not url:
                raise ValueError("附件缺少标识，请重新发送图片")
            if (digest and (digest, selection) in hashes) or (url and (url, selection) in urls):
                continue
            attachments.append(item)
            if digest: hashes.add((digest, selection))
            if url: urls.add((url, selection))
        if len(attachments) > MAX_COLLECTION_ATTACHMENTS:
            raise ValueError("一道收集题最多 12 个附件，请按题目拆分")
        return {**request, "text": text, "quoted": quoted, "attachments": attachments}

    def _check_active_limits(self, owner, max_active, max_user_active):
        slots = ",".join("?" for _ in ACTIVE)
        total = self.db.execute(f"SELECT count(*) FROM tasks WHERE state IN ({slots})", ACTIVE).fetchone()[0]
        own = self.db.execute(f"SELECT count(*) FROM tasks WHERE owner=? AND state IN ({slots})", (owner, *ACTIVE)).fetchone()[0]
        if total >= max_active: raise QueueFull("队列已满，请稍后再提交")
        if own >= max_user_active: raise QueueFull("你已有题目在处理中，请先查询或取消已有题目")

    def create(self, owner, message_id, kind, target, request, max_active=5,
               max_user_active=2, max_daily=50, state="queued"):
        if state not in ("queued", "collecting"):
            raise ValueError("新题初始状态无效")
        if state == "collecting": request = self._collection_materials(request)
        now = time.time()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            old = self.db.execute("SELECT * FROM tasks WHERE owner=? AND message_id=?", (owner, message_id)).fetchone()
            if old: return self._decode(old), False
            if state == "collecting":
                draft = self.collecting(owner)
                if draft:
                    raise QueueFull(f"你已有收集中题目 {draft['task_id']}，请先补图或开始。")
                total = self.db.execute("SELECT count(*) FROM tasks WHERE state='collecting'").fetchone()[0]
                if total >= max_active: raise QueueFull("收集题目已达到上限，请稍后再创建")
            else:
                self._check_active_limits(owner, max_active, max_user_active)
            recent = self.db.execute("SELECT count(*) FROM tasks WHERE owner=? AND created>?", (owner, now - 86400)).fetchone()[0]
            if recent >= max_daily: raise QueueFull("已达到最近 24 小时的题目预算")
            cursor = self.db.execute("""INSERT INTO tasks(owner,message_id,kind,target,created,updated,state,request)
                VALUES(?,?,?,?,?,?,?,?)""", (owner, message_id, kind, target, now, now, state, json.dumps(request, ensure_ascii=False)))
            day = datetime.now(timezone(timedelta(hours=8))).strftime("%y%m%d")
            task_id = f"Q{day}-{cursor.lastrowid:06d}"
            self.db.execute("UPDATE tasks SET task_id=? WHERE id=?", (task_id, cursor.lastrowid))
        return self.get(task_id), True

    def append_materials(self, task_id, owner, *, text="", quoted="", attachments=(), message_id=""):
        if not text and not quoted and not attachments:
            raise ValueError("请附上图片、引用材料或补充文字")
        incoming = list(attachments)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            task = self.get(task_id, owner)
            if task is None:
                raise ValueError("未找到你在本会话中的这个题号")
            if task["state"] != "collecting":
                raise ValueError("该题不在收集状态，请使用 /补充 题号 创建关联题")
            request = task["request"]
            messages = list(request.get("_material_message_ids") or [])
            if message_id and message_id in messages:
                return task, 0
            previous = self._collection_materials(request)
            updated = self._collection_materials({
                **request,
                "text": "\n\n".join(value for value in (previous["text"], str(text or "")) if value),
                "quoted": "\n\n".join(value for value in (previous["quoted"], str(quoted or "")) if value),
                "attachments": [*previous["attachments"], *incoming],
            })
            if collection_units(updated["attachments"]) > MAX_COLLECTION_UNITS:
                raise ValueError(
                    f"收集材料最多 {MAX_COLLECTION_UNITS} 张（图片按张、PDF 按页计）")
            added = len(updated["attachments"]) - len(previous["attachments"])
            if message_id and (text or quoted or added):
                messages.append(message_id)
                updated["_material_message_ids"] = messages
            self.db.execute("UPDATE tasks SET request=?,updated=? WHERE task_id=? AND owner=?",
                            (json.dumps(updated, ensure_ascii=False), time.time(), task["task_id"], owner))
        return self.get(task_id, owner), added

    def start_collection(self, task_id, owner, *, max_active=5, max_user_active=2, request=None):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            task = self.get(task_id, owner)
            if task is None:
                raise ValueError("未找到你在本会话中的这个题号")
            if task["state"] != "collecting":
                raise ValueError("该题已开始或不在收集状态，请按题号查询进度")
            if task["error"].startswith("首批附件未保存"):
                raise ValueError(f"{task['error']} 请用 /补图 {task['task_id']} 重新发送附件。")
            request = self._collection_materials(request if request is not None else task["request"])
            if not (request["text"].strip() or request["quoted"].strip() or request["attachments"]
                    or request.get("question_attachments")):
                raise ValueError("尚未收到题目材料，请先补图或补充文字")
            self._check_active_limits(owner, max_active, max_user_active)
            self.db.execute("""UPDATE tasks SET state='queued',request=?,updated=?,error='',delivery='pending'
                WHERE task_id=? AND owner=?""",
                            (json.dumps(request, ensure_ascii=False), time.time(), task["task_id"], owner))
        return self.get(task_id, owner)

    def update_collection_request(self, task_id, owner, request):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            task = self.get(task_id, owner)
            if task is None or task["state"] != "collecting":
                raise ValueError("题目已不在材料收集状态")
            request = self._collection_materials(request)
            self.db.execute("UPDATE tasks SET request=?,updated=? WHERE task_id=? AND owner=?",
                            (json.dumps(request, ensure_ascii=False), time.time(), task_id, owner))
        return self.get(task_id, owner)

    def update(self, task_id, **fields):
        if not fields.keys() <= {"state", "problem", "result", "error", "delivery", "question_ocr"}:
            raise ValueError("Invalid task fields")
        fields["updated"] = time.time()
        for field in ("result", "question_ocr"):
            if field in fields:
                fields[field] = json.dumps(fields[field], ensure_ascii=False)
        with self.db:
            self.db.execute("UPDATE tasks SET " + ",".join(f"{key}=?" for key in fields) + " WHERE task_id=?",
                            (*fields.values(), task_id))

    def recover(self):
        slots = ",".join("?" for _ in ACTIVE)
        drafts = self.db.execute(f"SELECT * FROM tasks WHERE state IN ({slots})", ACTIVE).fetchall()
        with self.db:
            self.db.execute(f"UPDATE tasks SET state='interrupted',error='服务重启，任务未自动重算；可按题号重试',updated=? WHERE state IN ({slots})",
                            (time.time(), *ACTIVE))
        for row in drafts:
            task = self._decode(row)
            result = task["result"]
            if result.get("text_path") and Path(result["text_path"]).is_file():
                result.update(answer_status="uncertain", verification="未完成（服务重启中断）")
                if result.get("solution"): result["solution"]["status"] = "uncertain"
                self.update(task["task_id"], state="completed", result=result,
                            error="服务重启：文字解答已保留，未重算。")

    def pending_book_ids(self, ttl_days):
        slots = ",".join("?" for _ in ACTIVE)
        rows = self.db.execute(f"""SELECT request FROM tasks WHERE state IN ({slots})
            OR (state='interrupted' AND created>=?)""", (*ACTIVE, time.time() - ttl_days * 86400))
        book_ids = set()
        for row in rows:
            request = json.loads(row["request"])
            for field in ("attachments", "question_attachments"):
                book_ids.update(item["book_id"] for item in (request.get(field) or ()) if item.get("book_id"))
        return book_ids

    def task_dir(self, task_id):
        if not re.fullmatch(TASK_RE, task_id): raise ValueError("Invalid task ID")
        path = (self.results / task_id).resolve()
        if path.parent != self.results: raise ValueError("Invalid result path")
        path.mkdir(exist_ok=True)
        return path

    @contextmanager
    def retain_result(self, task_id):
        self._retained_results[task_id] += 1
        try:
            yield
        finally:
            self._retained_results[task_id] -= 1
            if not self._retained_results[task_id]:
                del self._retained_results[task_id]

    def cleanup(self, ttl_days=7):
        threshold = time.time() - ttl_days * 86400
        rows = self.db.execute("SELECT task_id,state FROM tasks WHERE created<?",
                               (threshold,)).fetchall()
        slots = ",".join("?" for _ in ACTIVE)
        pending = self.db.execute(f"SELECT request FROM tasks WHERE state IN ({slots})", ACTIVE)
        dependencies = set()
        for row in pending:
            request = json.loads(row["request"])
            dependencies.add(request.get("parent_id"))
            for field in ("attachments", "question_attachments"):
                dependencies.update(item.get("source_task_id") for item in request.get(field, []))
        for row in rows:
            tid = row["task_id"]
            if row["state"] in ACTIVE or tid in self._retained_results or tid in dependencies:
                continue
            if not re.fullmatch(TASK_RE, tid):
                raise ValueError("Unsafe cleanup path")
            path = self.results / tid
            if path.is_symlink():
                path.unlink()
            elif path.exists():
                if path.resolve() != path:
                    raise ValueError("Unsafe cleanup path")
                shutil.rmtree(path)
            with self.db:
                self.db.execute("DELETE FROM tasks WHERE task_id=?", (tid,))
        self.invalidate_disk_bytes()

    def disk_bytes(self):
        """Return result bytes, cached briefly because callers issue bursts of checks."""
        now = time.monotonic()
        if now - self._disk_bytes_cache[0] < 2.0:
            return self._disk_bytes_cache[1]
        total = sum(p.stat().st_size for p in self.results.rglob("*") if p.is_file())
        self._disk_bytes_cache = (now, total)
        return total

    def invalidate_disk_bytes(self):
        self._disk_bytes_cache = (0.0, 0.0)


STATES = {"collecting": "收集中", "queued": "排队中", "reading": "读取题目", "planning": "确认题意", "searching": "检索资料",
          "calculating": "计算核验", "solving": "推导中", "verifying": "复核中", "rendering": "排版中",
          "completed": "已生成解答", "needs_input": "需要补充条件", "rejected": "不在解题范围",
          "failed": "处理失败", "cancelled": "已取消", "interrupted": "服务重启中断", "expired": "结果已过期"}

# Chat lines stay short: only the reason category, never the full explanation.
_VERIFICATION_SHORT = (
    ("模型复核通过", "复核通过"),
    ("复核发现问题并给出修订", "复核已修订"),
    ("存在疑点", "有疑点"),
    ("未进行复核", "未复核"),
)


def short_verification(text):
    """Compress the review status for one-line chat and PDF status lines."""
    text = " ".join(str(text or "").split())
    if not text:
        return ""
    for prefix, short in _VERIFICATION_SHORT:
        if text.startswith(prefix):
            return short
    match = re.match(r"未完成（([a-z_]+)）", text)
    if match:
        return "复核超时" if match.group(1) == "timeout" else f"复核未完成（{match.group(1)}）"
    return text[:24]


def describe(task):
    result = task["result"]
    tid = task["task_id"]
    state = STATES.get(task["state"], task["state"])
    if task["state"] == "completed" and result.get("study_text"):
        state = f"已生成{task['request']['action']}反馈"
    collecting = task["state"] == "collecting"
    lines = [] if collecting else [f"{tid} · {state}"]
    details = []
    if result.get("total_pages"):
        details.append(f"{result['total_pages']} 页")
    verification = short_verification(result.get("verification"))
    if result.get("answer_status") == "uncertain":
        details.append(verification or "有疑点")
    elif verification:
        details.append(verification)
    if details and not collecting:
        lines.append(" · ".join(details))
    if collecting:
        units = collection_units(task["request"].get("attachments") or [])
        lines.append(f"材料 {units} 张（最多 {MAX_COLLECTION_UNITS}）。")
        lines.append(f"/补图 {tid} 追加，/解题 {tid} 或 /查错 {tid} 开始。")
    if task["error"]:
        lines.append(task["error"])
    if task["state"] in ACTIVE:
        elapsed = max(0, int((time.time() - task["created"]) / 60))
        if elapsed:
            lines.append(f"已用 {elapsed} 分钟")
    return "\n".join(lines)
