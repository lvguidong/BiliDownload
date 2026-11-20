"""SQLite 持久化：任务队列、下载历史、配置缓存。"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional

from .models import (ACTIVE_STATUSES, STATUS_PENDING, DownloadTask,
                     TERMINAL_STATUSES)
from .utils import ensure_dir, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    episode TEXT NOT NULL,
    stream TEXT,
    output_path TEXT,
    work_dir TEXT,
    status TEXT NOT NULL,
    progress REAL DEFAULT 0,
    downloaded_bytes INTEGER DEFAULT 0,
    total_bytes INTEGER DEFAULT 0,
    speed REAL DEFAULT 0,
    retries INTEGER DEFAULT 0,
    error TEXT,
    created_at TEXT,
    finished_at TEXT,
    quality INTEGER DEFAULT 0,
    codec_pref TEXT DEFAULT 'AVC',
    extra TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

CREATE TABLE IF NOT EXISTS history (
    key TEXT PRIMARY KEY,       -- aid:cid 去重键
    task_id TEXT,
    output_path TEXT,
    media_id TEXT,
    title TEXT,
    downloaded_at TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class Storage:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        ensure_dir(os.path.dirname(self.path))
        self._local = threading.local()
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def _init(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    # ---------- tasks ----------
    def upsert_task(self, task: DownloadTask) -> None:
        row = task.to_row()
        cols = list(row.keys())
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "task_id")
        sql = f"INSERT INTO tasks ({','.join(cols)}) VALUES ({placeholders}) " \
              f"ON CONFLICT(task_id) DO UPDATE SET {updates}"
        with self._conn() as conn:
            conn.execute(sql, [row[c] for c in cols])

    def get_task(self, task_id: str) -> Optional[DownloadTask]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return DownloadTask.from_row(dict(row)) if row else None

    def list_tasks(self, statuses: Optional[List[str]] = None) -> List[DownloadTask]:
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            sql = f"SELECT * FROM tasks WHERE status IN ({placeholders}) ORDER BY created_at"
            params: List[Any] = statuses
        else:
            sql = "SELECT * FROM tasks ORDER BY created_at"
            params = []
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [DownloadTask.from_row(dict(row)) for row in rows]

    def count_tasks(self, status: str) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE status=?", (status,)).fetchone()
        return int(row["n"]) if row else 0

    def delete_task(self, task_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))

    def clear_tasks(self, only_terminal: bool = False) -> None:
        with self._conn() as conn:
            if only_terminal:
                placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
                conn.execute(f"DELETE FROM tasks WHERE status IN ({placeholders})", TERMINAL_STATUSES)
            else:
                conn.execute("DELETE FROM tasks")

    def mark_interrupted(self) -> int:
        """把上次异常退出时残留的 downloading/merging 任务重置为 pending。"""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE tasks SET status=?, error=? WHERE status IN ('downloading','merging')",
                (STATUS_PENDING, "程序重启，任务已从断点恢复"),
            )
            return cur.rowcount

    # ---------- history ----------
    @staticmethod
    def dedup_key(aid: int, cid: int) -> str:
        return f"{aid}:{cid}"

    def is_downloaded(self, aid: int, cid: int) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM history WHERE key=?", (self.dedup_key(aid, cid),)
            ).fetchone()
        return row is not None

    def add_history(self, task: DownloadTask, media_id: str = "") -> None:
        key = self.dedup_key(task.episode.aid, task.episode.cid)
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO history (key,task_id,output_path,media_id,title,downloaded_at) "
                "VALUES (?,?,?,?,?,?)",
                (key, task.task_id, task.output_path, media_id,
                 task.episode.display_title, now_iso()),
            )

    def list_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM history ORDER BY downloaded_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    # ---------- meta ----------
    def set_meta(self, key: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key,value) VALUES (?,?)", (key, value)
            )

    def get_meta(self, key: str, default: str = "") -> str:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default
