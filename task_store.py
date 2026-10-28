"""Durable, owner-scoped tasks. No message content is stored in logs."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
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


class QueueFull(ValueError):
    pass


class TaskStore:
    def __init__(self, directory: Path):
        self.root = directory.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.results = self.root / "results"
        self.results.mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.root / "tasks.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT UNIQUE,
            owner TEXT NOT NULL, message_id TEXT NOT NULL, kind TEXT NOT NULL, target TEXT NOT NULL,
            created REAL NOT NULL, updated REAL NOT NULL, state TEXT NOT NULL,
            request TEXT NOT NULL, problem TEXT NOT NULL DEFAULT '',
            result TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
            delivery TEXT NOT NULL DEFAULT 'pending', UNIQUE(owner,message_id))""")
        self.db.commit()

    def close(self):
        self.db.close()

    @staticmethod
    def _decode(row):
        if row is None: return None
        task = dict(row)
        task["request"] = json.loads(task["request"])
        task["result"] = json.loads(task["result"])
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

    def collecting(self, owner):
        return self._decode(self.db.execute(
            "SELECT * FROM tasks WHERE owner=? AND state='collecting' ORDER BY id DESC LIMIT 1",
            (owner,)).fetchone())

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
            digest, url = str(item.get("sha256") or ""), str(item.get("url") or "")
            if not digest and not url:
                raise ValueError("附件缺少标识，请重新发送图片")
            if (digest and digest in hashes) or (url and url in urls):
                continue
            attachments.append(item)
            if digest: hashes.add(digest)
            if url: urls.add(url)
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
                    raise QueueFull(f"你已有收集中的题目 {draft['task_id']}，请继续补图或先开始、取消该题")
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
            added = len(updated["attachments"]) - len(previous["attachments"])
            if message_id and (text or quoted or added):
                messages.append(message_id)
                updated["_material_message_ids"] = messages
            self.db.execute("UPDATE tasks SET request=?,updated=? WHERE task_id=? AND owner=?",
                            (json.dumps(updated, ensure_ascii=False), time.time(), task["task_id"], owner))
        return self.get(task_id, owner), added

    def start_collection(self, task_id, owner, *, max_active=5, max_user_active=2):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            task = self.get(task_id, owner)
            if task is None:
                raise ValueError("未找到你在本会话中的这个题号")
            if task["state"] != "collecting":
                raise ValueError("该题已开始或不在收集状态，请按题号查询进度")
            if task["error"].startswith("首批附件未保存"):
                raise ValueError(f"{task['error']} 请用 /补图 {task['task_id']} 重新发送附件。")
            request = self._collection_materials(task["request"])
            if not (request["text"].strip() or request["quoted"].strip() or request["attachments"]):
                raise ValueError("尚未收到题目材料，请先补图或补充文字")
            self._check_active_limits(owner, max_active, max_user_active)
            self.db.execute("UPDATE tasks SET state='queued',updated=?,error='',delivery='pending' WHERE task_id=? AND owner=?",
                            (time.time(), task["task_id"], owner))
        return self.get(task_id, owner)

    def update(self, task_id, **fields):
        if not fields.keys() <= {"state", "problem", "result", "error", "delivery"}:
            raise ValueError("Invalid task fields")
        fields["updated"] = time.time()
        if "result" in fields: fields["result"] = json.dumps(fields["result"], ensure_ascii=False)
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
                            error="服务重启，已保留文字解答；未自动重算，尚不能视为已解决。")

    def task_dir(self, task_id):
        if not re.fullmatch(TASK_RE, task_id): raise ValueError("Invalid task ID")
        path = (self.results / task_id).resolve()
        if path.parent != self.results: raise ValueError("Invalid result path")
        path.mkdir(exist_ok=True)
        return path

    def cleanup(self, ttl_days=7):
        threshold = time.time() - ttl_days * 86400
        rows = self.db.execute("SELECT task_id,state FROM tasks WHERE created<? AND state!='expired'", (threshold,)).fetchall()
        for row in rows:
            if row["state"] in ACTIVE: continue
            path = (self.results / row["task_id"]).resolve()
            if path.parent != self.results: raise ValueError("Unsafe cleanup path")
            if path.exists(): shutil.rmtree(path)
            with self.db:
                self.db.execute("UPDATE tasks SET state='expired',request='{}',problem='',result='{}',error='' WHERE task_id=?", (row["task_id"],))

    def disk_bytes(self):
        return sum(p.stat().st_size for p in self.results.rglob("*") if p.is_file())


STATES = {"collecting": "收集中", "queued": "排队中", "reading": "读取题目", "planning": "确认题意", "searching": "检索资料",
          "calculating": "计算核验", "solving": "推导中", "verifying": "复核中", "rendering": "排版中",
          "completed": "已生成解答", "needs_input": "需要补充条件", "rejected": "不在解题范围",
          "failed": "处理失败", "cancelled": "已取消", "interrupted": "服务重启中断", "expired": "结果已过期"}


def describe(task):
    result = task["result"]
    lines = [f"{task['task_id']}｜{STATES.get(task['state'], task['state'])}"]
    if task["state"] == "collecting":
        count = len(task["request"].get("attachments") or [])
        lines.append(f"已收集 {count} 个附件，最多 12 个；尚未开始解题。")
        lines.append(f"/补图 {task['task_id']}（同条消息附图片）")
        lines.append(f"/开始 {task['task_id']}（收齐材料后解题）")
    if result.get("answer_status") == "uncertain": lines.append("结论：仍有疑点，不能视为已解决")
    if result.get("verification"): lines.append("复核：" + result["verification"])
    if result.get("total_pages"): lines.append(f"完整 PDF：{result['total_pages']} 页")
    if task["error"]: lines.append(task["error"])
    if task["state"] in ACTIVE:
        lines.append(f"自提交起已过 {max(0,int((time.time()-task['created'])/60))} 分钟（含排队）")
    return "\n".join(lines)
