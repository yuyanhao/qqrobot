"""Persistent PDF catalog shared by the bot's conversations."""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import os
from pathlib import Path
import re
import shutil
from shutil import copyfile, rmtree
import sqlite3
from tempfile import TemporaryDirectory
from uuid import uuid4

# Load .env before pdf_tools computes optional limits at import time.
from qqrobot.settings import Settings

from qqrobot.media.pdf_tools import PDFError, pdf_page_count, _select_pages

BOOK_ID = r"B[0-9]{4,}"
BOOK_UPLOAD_DISK_RESERVE_MB = 64


def _env_limit(name, default, low, high):
    try:
        value = int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


MAX_BOOK_PDF_MB = _env_limit("BOOK_MAX_PDF_MB", 100, 1, 2048)
MAX_BOOK_LIBRARY_MB = _env_limit("BOOK_LIBRARY_MAX_MB", 1024, 1, 10240)
_BOOK_REQUEST = re.compile(rf"^\s*({BOOK_ID})\s+([\s\S]+)$", re.I)
_BOOK_PAGE_SPEC = re.compile(r"(?:第\s*)?[+\-]?\d[\d\s.第页+\-–—~～至到、,，和及/]*页", re.I)
_QUESTION_TARGET = re.compile(r"第\s*(?:\d{1,3}|[〇零一二三四五六七八九十百千万两]+)\s*(?:道)?题")


class BookLibrary:
    def __init__(self, root):
        self.directory = root / "books"

    def _connect(self):
        self.directory.mkdir(exist_ok=True, parents=True)
        db = sqlite3.connect(self.directory / "catalog.sqlite3")
        db.row_factory = sqlite3.Row
        db.execute("""CREATE TABLE IF NOT EXISTS books (
            id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT NOT NULL,
            title TEXT NOT NULL, summary TEXT NOT NULL, pages INTEGER NOT NULL,
            removed INTEGER NOT NULL DEFAULT 0
        )""")
        if "removed" not in {row["name"] for row in db.execute("PRAGMA table_info(books)")}:
            with db:
                db.execute("BEGIN IMMEDIATE")
                columns = {row["name"] for row in db.execute("PRAGMA table_info(books)")}
                if "removed" not in columns:
                    db.execute("ALTER TABLE books ADD COLUMN removed INTEGER NOT NULL DEFAULT 0")
        return db

    @staticmethod
    def _entry(row, public_id=None):
        entry = dict(row)
        storage_id = f"B{entry['id']:04d}"
        entry["id"] = public_id or storage_id
        entry["_storage_id"] = storage_id
        return entry

    @contextmanager
    def staged_pdf(self):
        self.directory.mkdir(exist_ok=True, parents=True)
        with TemporaryDirectory(prefix=".book-import-", dir=self.directory) as directory:
            yield Path(directory) / "book.pdf"

    def cleanup_staged(self):
        """Remove temporary import directories left behind by a crash."""
        if not self.directory.exists():
            return
        for path in self.directory.glob(".book-import-*"):
            if path.is_dir() and not path.is_symlink():
                rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)

    def stored_bytes(self, *, active_only=True):
        """Bytes used by active books (or by every PDF left in the directory)."""
        if not self.directory.exists():
            return 0
        if active_only:
            with closing(self._connect()) as db:
                names = {row["filename"] for row in db.execute(
                    "SELECT filename FROM books WHERE removed=0")}
            paths = [self.directory / name for name in names]
        else:
            paths = list(self.directory.glob("*.pdf"))
        total = 0
        for path in paths:
            try:
                if path.is_file() and not path.is_symlink():
                    total += path.stat().st_size
            except OSError:
                continue
        return total

    def add(self, staged_pdf: Path, title, summary=""):
        """Move a validated, staged PDF into the library and publish its catalog entry."""
        title, summary = " ".join(title.split()), " ".join(summary.split())
        if not title or len(title) > 80 or len(summary) > 100:
            raise ValueError("请填写书名（最多 80 字）和可选简介（最多 100 字）。")
        size = staged_pdf.stat().st_size
        if not size:
            raise PDFError("书库 PDF 为空。")
        if size > MAX_BOOK_PDF_MB * 1024**2:
            raise PDFError(f"书库 PDF 不能超过 {MAX_BOOK_PDF_MB} MB。")
        if self.stored_bytes() + size > MAX_BOOK_LIBRARY_MB * 1024**2:
            raise PDFError(f"书库总容量不能超过 {MAX_BOOK_LIBRARY_MB} MB，请先移除不再使用的书籍。")
        pages = pdf_page_count(staged_pdf)
        filename = uuid4().hex + ".pdf"
        path = self.directory / filename
        with closing(self._connect()) as db:
            try:
                with db:
                    number = db.execute(
                        "INSERT INTO books(filename,title,summary,pages) VALUES(?,?,?,?)",
                        (filename, title, summary, pages),
                    ).lastrowid
                    staged_pdf.replace(path)
                    row = db.execute("SELECT * FROM books WHERE id=?", (number,)).fetchone()
                    position = db.execute("SELECT COUNT(*) FROM books WHERE removed=0").fetchone()[0]
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            return self._entry(row, public_id=f"B{position:04d}")

    def list(self, limit, offset=0):
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT * FROM books WHERE removed=0 ORDER BY id LIMIT ? OFFSET ?", (limit, offset)).fetchall()
            return [self._entry(row, public_id=f"B{offset + index:04d}")
                    for index, row in enumerate(rows, 1)]

    def get_by_storage_id(self, book_id, *, include_removed=False):
        """Look up by the stable internal id (B0001 in filenames and task requests)."""
        if not re.fullmatch(BOOK_ID, book_id, flags=re.I) or len(book_id) > 18:
            raise ValueError("书籍编号格式无效，例如 B0001。")
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM books WHERE id=? AND (removed=0 OR ?)",
                             (int(book_id[1:]), include_removed)).fetchone()
        if row is None:
            raise ValueError(f"书库里没有 {book_id.upper()}。")
        return self._entry(row)

    def get(self, book_id, *, include_removed=False):
        """Legacy alias for get_by_storage_id."""
        return self.get_by_storage_id(book_id, include_removed=include_removed)

    def get_by_display_id(self, book_id):
        """Look up by the current user-visible id; it shifts after a removal."""
        if not re.fullmatch(BOOK_ID, book_id, flags=re.I) or len(book_id) > 18:
            raise ValueError("书籍编号格式无效，例如 B0001。")
        position = int(book_id[1:])
        if position < 1:
            raise ValueError(f"书库里没有 {book_id.upper()}。")
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM books WHERE removed=0 ORDER BY id LIMIT 1 OFFSET ?",
                             (position - 1,)).fetchone()
        if row is None:
            raise ValueError(f"书库里没有 {book_id.upper()}。")
        return self._entry(row, public_id=f"B{position:04d}")

    def get_public(self, book_id):
        """Legacy alias for get_by_display_id."""
        return self.get_by_display_id(book_id)

    def path_for_storage_id(self, book_id, *, include_removed=False):
        path = self.directory / self.get_by_storage_id(
            book_id, include_removed=include_removed)["filename"]
        if path.is_symlink() or path.resolve().parent != self.directory.resolve() or not path.is_file():
            raise ValueError("书籍 PDF 不可用，请联系管理员。")
        return path

    def path(self, book_id, *, include_removed=False):
        """Legacy alias for path_for_storage_id."""
        return self.path_for_storage_id(book_id, include_removed=include_removed)

    def remove(self, book_id):
        book = self.get_by_display_id(book_id)
        with closing(self._connect()) as db, db:
            changed = db.execute("UPDATE books SET removed=1 WHERE id=? AND removed=0",
                                 (int(book["_storage_id"][1:]),)).rowcount
            if not changed:
                raise ValueError(f"书库里没有 {book_id.upper()}。")
        return book

    def purge_removed(self, pending_book_ids):
        if not (self.directory / "catalog.sqlite3").is_file():
            return
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute("SELECT id,filename FROM books WHERE removed=1").fetchall():
                if f"B{row['id']:04d}" in pending_book_ids:
                    continue
                path = self.directory / row["filename"]
                if path.parent != self.directory:
                    raise ValueError("书籍 PDF 路径无效。")
                path.unlink(missing_ok=True)
                db.execute("DELETE FROM books WHERE id=?", (row["id"],))

    def reference(self, args):
        match = _BOOK_REQUEST.fullmatch(args)
        if not match:
            raise ValueError("请填写书籍编号和物理页码，例如 B0001 第3-5页。")
        book_id, request = match.groups()
        page = _BOOK_PAGE_SPEC.search(request)
        if not page:
            raise ValueError("请填写书籍编号和物理页码，例如 B0001 第3-5页。")
        spec = page.group().strip()
        question = _QUESTION_TARGET.search(request)
        book = self.get_by_display_id(book_id)
        _, selected = _select_pages(spec, book["pages"])
        if not selected:
            raise PDFError("引用书籍时请指定页码。")
        tail = request
        if question:
            tail = tail[:question.start()] + tail[question.end():]
        remaining_page = _BOOK_PAGE_SPEC.search(tail)
        if remaining_page:
            tail = tail[:remaining_page.start()] + tail[remaining_page.end():]
        tail = re.sub(r"^\s*(?:的|[，,。:：、])\s*|\s*(?:的|[，,。:：、])\s*$", "", tail).strip()
        item = {"book_id": book["_storage_id"], "name": book["title"] + ".pdf",
                "pdf_selection": spec}
        if question:
            item["question_target"] = question.group().strip()
        return item, tail


def main():
    parser = argparse.ArgumentParser(description="管理云端共享 PDF 书库")
    parser.add_argument("--data-dir", type=Path, default=Settings().data_dir)
    actions = parser.add_subparsers(dest="action", required=True)
    add = actions.add_parser("add")
    add.add_argument("pdf", type=Path)
    add.add_argument("title")
    add.add_argument("--summary", default="")
    remove = actions.add_parser("remove")
    remove.add_argument("id")
    actions.add_parser("list")
    args = parser.parse_args()
    library = BookLibrary(args.data_dir)
    try:
        if args.action == "add":
            if not args.pdf.is_file() or args.pdf.stat().st_size > MAX_BOOK_PDF_MB * 1024**2:
                raise PDFError(f"书库 PDF 必须存在且不能超过 {MAX_BOOK_PDF_MB} MB。")
            args.data_dir.mkdir(parents=True, exist_ok=True)
            size_mb = (args.pdf.stat().st_size + 1024**2 - 1) // 1024**2
            free_mb = shutil.disk_usage(args.data_dir).free // 1024**2
            if free_mb - BOOK_UPLOAD_DISK_RESERVE_MB < max(1, size_mb):
                raise PDFError(f"磁盘可用空间不足；需要保留 {BOOK_UPLOAD_DISK_RESERVE_MB} MB。")
            with library.staged_pdf() as staged:
                copyfile(args.pdf, staged)
                book = library.add(staged, args.title, args.summary)
            print(f"已入库 {book['id']} 《{book['title']}》，共 {book['pages']} 页。")
        elif args.action == "remove":
            book = library.remove(args.id)
            if not (args.data_dir / "tasks.sqlite3").exists():
                library.purge_removed(set())
            print(f"已移除 {book['id']}。")
        else:
            for book in library.list(-1):
                print(book["id"], book["title"], f"{book['pages']} 页", book["summary"])
    except (PDFError, ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"书库操作失败：{exc}\n")


if __name__ == "__main__":
    main()
