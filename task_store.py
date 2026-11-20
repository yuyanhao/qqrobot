"""Durable, owner-scoped tasks. No message content is stored in logs."""
from __future__ import annotations
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import sqlite3
import time

ACTIVE = ("queued", "reading", "planning", "searching", "calculating", "solving", "verifying", "rendering")
TASK_RE = r"Q\d{6}-\d{6,10}"
MAX_COLLECTION_ATTACHMENTS = 12
MAX_COLLECTION_TEXT = 24000
MAX_FAVORITES = 100
MAX_FAVORITE_NOTE = 300


def confirmation_key(task_id, problem, ocr):
    revision = json.dumps([task_id, problem, ocr], ensure_ascii=False, sort_keys=True)
    return "confirm:" + sha256(revision.encode("utf-8")).hexdigest()


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
            self.db.execute("""CREATE TABLE IF NOT EXISTS reusable_tasks (
                owner TEXT NOT NULL, reuse_key TEXT NOT NULL, task_id TEXT NOT NULL UNIQUE,
                PRIMARY KEY(owner,reuse_key))""")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("""CREATE TABLE IF NOT EXISTS favorites (
                owner TEXT NOT NULL, task_id TEXT NOT NULL, saved REAL NOT NULL,
                problem TEXT NOT NULL, answer TEXT NOT NULL, note TEXT NOT NULL,
                PRIMARY KEY(owner,task_id))""")
            columns = {row["name"] for row in self.db.execute("PRAGMA table_info(favorites)")}
            if "question_ocr" not in columns:
                self.db.execute("ALTER TABLE favorites ADD COLUMN question_ocr TEXT NOT NULL DEFAULT '{}'")
            self.db.execute("CREATE INDEX IF NOT EXISTS favorites_owner_saved ON favorites(owner, saved)")
            self._restore_confirmation_links()

    def _restore_confirmation_links(self):
        # Earlier releases saved the confirmation key, which includes the old ID.
        rows = self.db.execute("""SELECT tasks.task_id,tasks.owner,tasks.request,reuse_key
            FROM reusable_tasks JOIN tasks USING(owner,task_id)
            WHERE reuse_key LIKE 'confirm:%' AND json_extract(request,'$.confirmation_of') IS NULL
            AND tasks.state!='expired'""").fetchall()
        if not rows:
            return
        pending = defaultdict(list)
        for task in self.db.execute("""SELECT task_id,owner FROM tasks
                WHERE json_extract(question_ocr,'$.needs_confirmation')=1"""):
            pending[task["owner"]].append(task["task_id"])
        for row in rows:
            request = json.loads(row["request"])
            problem = request.get("problem_snapshot", request.get("text", ""))
            ocr = request.get("question_ocr", {})
            for task_id in pending[row["owner"]]:
                if row["reuse_key"] == confirmation_key(task_id, problem, ocr):
                    self.db.execute("""UPDATE tasks SET request=json_set(request,'$.confirmation_of',?)
                        WHERE task_id=?""", (task_id, row["task_id"]))
                    break

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

    def recent_question_choices(self, owner, *, ttl_days=7, states=(), needs_confirmation=False,
                                ready_to_check=False, limit=3):
        threshold = time.time() - ttl_days * 86400
        active_slots = ",".join("?" for _ in ACTIVE)
        sql = f"""SELECT task_id,state,created FROM tasks WHERE owner=? AND state!='expired'
            AND (created>=? OR state IN ({active_slots}) OR EXISTS (
                SELECT 1 FROM favorites WHERE favorites.owner=tasks.owner AND favorites.task_id=tasks.task_id
            )) AND NOT EXISTS (
                SELECT 1 FROM tasks AS continued WHERE continued.owner=tasks.owner AND continued.state!='expired'
                AND json_extract(continued.request,'$.confirmation_of')=tasks.task_id
            )"""
        params = [owner, threshold, *ACTIVE]
        if states:
            sql += " AND state IN (" + ",".join("?" for _ in states) + ")"
            params.extend(states)
        if needs_confirmation:
            sql += " AND json_extract(question_ocr, '$.needs_confirmation')=1"
        if ready_to_check:
            sql += """ AND (
                (problem!='' AND coalesce(json_extract(question_ocr,'$.needs_confirmation'),0)!=1)
                OR (problem='' AND EXISTS (
                    SELECT 1 FROM favorites WHERE favorites.owner=tasks.owner AND favorites.task_id=tasks.task_id
                    AND favorites.problem!=''
                    AND coalesce(json_extract(favorites.question_ocr,'$.needs_confirmation'),0)!=1
                )))"""
        return self.db.execute(sql + " ORDER BY id DESC LIMIT ?", [*params, int(limit)]).fetchall()

    def confirmation_successor(self, task_id, owner):
        row = self.db.execute("""SELECT task_id FROM tasks
            WHERE owner=? AND state!='expired' AND json_extract(request,'$.confirmation_of')=?
            ORDER BY id DESC LIMIT 1""", (owner, task_id)).fetchone()
        return row["task_id"] if row else None

    def get_reusable(self, owner, reuse_key):
        row = self.db.execute("""SELECT tasks.* FROM reusable_tasks
            JOIN tasks ON tasks.task_id=reusable_tasks.task_id AND tasks.owner=reusable_tasks.owner
            WHERE reusable_tasks.owner=? AND reuse_key=? AND tasks.state!='expired'""",
            (owner, reuse_key)).fetchone()
        return self._decode(row)

    @staticmethod
    def _decode_favorite(row):
        if row is None:
            return None
        favorite = dict(row)
        favorite["question_ocr"] = json.loads(favorite["question_ocr"])
        return favorite

    def get_favorite(self, task_id, owner):
        row = self.db.execute("SELECT * FROM favorites WHERE owner=? AND task_id=?",
                              (owner, task_id.upper())).fetchone()
        return self._decode_favorite(row)

    def list_favorites(self, owner, limit, offset=0):
        return [dict(row) for row in self.db.execute(
            "SELECT task_id,substr(problem,1,72) AS preview FROM favorites WHERE owner=? "
            "ORDER BY saved DESC,task_id DESC LIMIT ? OFFSET ?", (owner, limit, offset))]

    def favorites_in_order(self, owner, *, limit=-1, offset=0):
        rows = self.db.execute(
            "SELECT * FROM favorites WHERE owner=? ORDER BY saved,task_id LIMIT ? OFFSET ?", (owner, limit, offset))
        return [self._decode_favorite(row) for row in rows]

    def save_favorite(self, task_id, owner, problem, answer, note=None, *, question_ocr=None):
        if note is not None and len(note) > MAX_FAVORITE_NOTE:
            raise ValueError(f"错因最多 {MAX_FAVORITE_NOTE} 字符。")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            previous = self.get_favorite(task_id, owner)
            if previous:
                if note is None:
                    note = previous["note"]
                if problem == previous["problem"]:
                    if not answer:
                        answer = previous["answer"]
                    if question_ocr is None:
                        question_ocr = previous["question_ocr"]
            else:
                count = self.db.execute("SELECT count(*) FROM favorites WHERE owner=?", (owner,)).fetchone()[0]
                if count >= MAX_FAVORITES:
                    raise ValueError(f"本会话已收藏 {MAX_FAVORITES} 道题，可取消部分收藏后再添加。")
            self.db.execute("""INSERT INTO favorites(owner,task_id,saved,problem,answer,note,question_ocr)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(owner,task_id) DO UPDATE SET problem=excluded.problem,
                answer=excluded.answer,note=excluded.note,question_ocr=excluded.question_ocr""",
                (owner, task_id.upper(), time.time(), problem, answer, note or "",
                 json.dumps(question_ocr or {}, ensure_ascii=False)))

    def fill_favorite_answer(self, task_id, owner, problem, answer):
        with self.db:
            self.db.execute("UPDATE favorites SET answer=? WHERE owner=? AND task_id=? AND problem=? AND answer=''",
                            (answer, owner, task_id.upper(), problem))

    def update_favorite_question(self, task_id, owner, problem, question_ocr):
        with self.db:
            self.db.execute("""UPDATE favorites SET problem=?,question_ocr=?
                WHERE task_id=? AND owner=? AND answer='' AND
                (problem='' OR json_extract(question_ocr,'$.needs_confirmation')=1)""",
                            (problem, json.dumps(question_ocr, ensure_ascii=False), task_id, owner))

    def remove_favorite(self, task_id, owner):
        with self.db:
            return bool(self.db.execute("DELETE FROM favorites WHERE owner=? AND task_id=?",
                                        (owner, task_id.upper())).rowcount)

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
        reuse_key = request.get("reuse_key") if state == "queued" else None
        now = time.time()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            old = self.db.execute("SELECT * FROM tasks WHERE owner=? AND message_id=?", (owner, message_id)).fetchone()
            if old: return self._decode(old), False
            if reuse_key:
                reusable = self.get_reusable(owner, reuse_key)
                if reusable and (not request.get("retry_of") or reusable["state"] in ACTIVE):
                    return reusable, False
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
            if reuse_key:
                self.db.execute("""INSERT INTO reusable_tasks(owner,reuse_key,task_id) VALUES(?,?,?)
                    ON CONFLICT(owner,reuse_key) DO UPDATE SET task_id=excluded.task_id""",
                    (owner, reuse_key, task_id))
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
        rows = self.db.execute("""SELECT task_id,state FROM tasks WHERE created<?
            AND NOT EXISTS(SELECT 1 FROM favorites WHERE favorites.task_id=tasks.task_id AND favorites.owner=tasks.owner)""",
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
                self.db.execute("DELETE FROM reusable_tasks WHERE task_id=?", (tid,))
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
    lines = [f"{tid} · {state}"]
    details = []
    if result.get("total_pages"):
        details.append(f"{result['total_pages']} 页")
    verification = short_verification(result.get("verification"))
    if result.get("answer_status") == "uncertain":
        details.append(verification or "有疑点")
    elif verification:
        details.append(verification)
    if details:
        lines.append(" · ".join(details))
    if task["state"] == "collecting":
        count = len(task["request"].get("attachments") or [])
        lines.append(f"材料 {count} 个（最多 12）。")
        lines.append(f"/补图 {tid} 追加，/解题 {tid} 或 /查错 {tid} 开始。")
    if task["error"]:
        lines.append(task["error"])
    if task["state"] in ACTIVE:
        elapsed = max(0, int((time.time() - task["created"]) / 60))
        if elapsed:
            lines.append(f"已用 {elapsed} 分钟")
    return "\n".join(lines)
