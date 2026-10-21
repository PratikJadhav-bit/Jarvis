"""
core/task_queue.py — Persistent SQLite task queue for Mark-LIII background agent.

Stores tasks with full lifecycle tracking:
  - id: unique string identifier (e.g. task_20261021_120000_a1b2)
  - goal: user prompt / objective
  - status: PENDING | RUNNING | COMPLETED | FAILED | CANCELLED
  - created: unix timestamp
  - updated: unix timestamp
  - steps: JSON-encoded list of executed step records
  - result: final result text / summary
  - error: error message if failed / cancelled
  - unspoken: 1 if user hasn't heard spoken summary yet, 0 once spoken
"""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


from core import db_paths


_DB_PATH = db_paths.get_db_path("tasks.db")
_local = threading.local()
_write_lock = threading.Lock()
_conn_lock = threading.Lock()
_open_connections: set[sqlite3.Connection] = set()


def set_db_path(path: Path) -> None:
    """Override database path (used by tests)."""
    global _DB_PATH
    close_all_connections()
    _DB_PATH = path
    if hasattr(_local, "con"):
        _local.con = None


def get_db(db_path: Optional[Path] = None) -> sqlite3.Connection:
    target = db_path or _DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(target), timeout=10.0, check_same_thread=False)
    con.row_factory = sqlite3.Row
    with _conn_lock:
        _open_connections.add(con)
    return con


def _conn() -> sqlite3.Connection:
    if not hasattr(_local, "con") or _local.con is None:
        _local.con = get_db()
        _init_db(_local.con)
    return _local.con


def close_all_connections() -> None:
    """Close all open connections across all threads (for tests and teardown)."""
    with _conn_lock:
        for con in list(_open_connections):
            try:
                con.close()
            except Exception:
                pass
        _open_connections.clear()
    if hasattr(_local, "con"):
        _local.con = None


def _init_db(con: sqlite3.Connection) -> None:
    with _write_lock:
        with con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    goal TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created REAL NOT NULL,
                    updated REAL NOT NULL,
                    steps TEXT DEFAULT '[]',
                    result TEXT,
                    error TEXT,
                    unspoken INTEGER DEFAULT 1
                );
            """)
            con.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);")
            con.execute("CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created);")
            con.execute("CREATE INDEX IF NOT EXISTS idx_tasks_unspoken ON tasks(unspoken);")


def set_custom_db_path(path: Path) -> None:
    """Useful in unit tests to point to an isolated or in-memory database."""
    global _DB_PATH
    close_all_connections()
    _DB_PATH = path


set_db_path = set_custom_db_path


def generate_task_id() -> str:
    ts = time.strftime("%Y%m%d_%H%M%S")
    rand = uuid.uuid4().hex[:6]
    return f"task_{ts}_{rand}"


def create_task(goal: str, task_id: Optional[str] = None) -> str:
    """Insert a new task in PENDING status and return its ID."""
    tid = task_id or generate_task_id()
    now = time.time()
    con = _conn()
    with _write_lock:
        with con:
            con.execute("""
                INSERT INTO tasks (id, goal, status, created, updated, steps, result, error, unspoken)
                VALUES (?, ?, 'PENDING', ?, ?, '[]', NULL, NULL, 1)
            """, (tid, goal, now, now))
    return tid


def get_task(task_id: str) -> Optional[dict[str, Any]]:
    """Return task row as a dict, or None if not found."""
    con = _conn()
    row = con.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["steps"] = json.loads(d.get("steps") or "[]")
    except Exception:
        d["steps"] = []
    return d


def get_latest_task() -> Optional[dict[str, Any]]:
    """Return the most recently created task, or None."""
    con = _conn()
    row = con.execute("SELECT * FROM tasks ORDER BY created DESC LIMIT 1").fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["steps"] = json.loads(d.get("steps") or "[]")
    except Exception:
        d["steps"] = []
    return d


def list_tasks(status: Optional[str] = None, limit: int = 10) -> list[dict[str, Any]]:
    """List recent tasks, optionally filtered by status (PENDING, RUNNING, etc.)."""
    con = _conn()
    if status and status.lower() != "all":
        rows = con.execute(
            "SELECT * FROM tasks WHERE UPPER(status) = UPPER(?) ORDER BY created DESC LIMIT ?",
            (status, limit)
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT * FROM tasks ORDER BY created DESC LIMIT ?",
            (limit,)
        ).fetchall()

    tasks = []
    for r in rows:
        d = dict(r)
        try:
            d["steps"] = json.loads(d.get("steps") or "[]")
        except Exception:
            d["steps"] = []
        tasks.append(d)
    return tasks


def update_task(
    task_id: str,
    status: Optional[str] = None,
    steps: Optional[list[dict]] = None,
    result: Optional[str] = None,
    error: Optional[str] = None,
    unspoken: Optional[int] = None,
) -> bool:
    """Update fields of an existing task."""
    con = _conn()
    fields = ["updated = ?"]
    params: list[Any] = [time.time()]

    if status is not None:
        fields.append("status = ?")
        params.append(status.upper())
    if steps is not None:
        fields.append("steps = ?")
        params.append(json.dumps(steps, ensure_ascii=False))
    if result is not None:
        fields.append("result = ?")
        params.append(result)
    if error is not None:
        fields.append("error = ?")
        params.append(error)
    if unspoken is not None:
        fields.append("unspoken = ?")
        params.append(int(unspoken))

    params.append(task_id)
    sql = f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?"
    with _write_lock:
        with con:
            cur = con.execute(sql, params)
            return cur.rowcount > 0


def cancel_task(task_id: str) -> bool:
    """Cancel a PENDING or RUNNING task. Returns True if cancelled."""
    if task_id.lower() == "latest":
        latest = get_latest_task()
        if not latest:
            return False
        task_id = latest["id"]

    con = _conn()
    now = time.time()
    with _write_lock:
        with con:
            cur = con.execute("""
                UPDATE tasks
                SET status = 'CANCELLED', updated = ?, error = 'Cancelled by user'
                WHERE id = ? AND status IN ('PENDING', 'RUNNING')
            """, (now, task_id))
            return cur.rowcount > 0


def cancel_all_active_tasks() -> int:
    """Cancel all PENDING and RUNNING tasks (used by emergency stop). Returns count."""
    con = _conn()
    now = time.time()
    with _write_lock:
        with con:
            cur = con.execute("""
                UPDATE tasks
                SET status = 'CANCELLED', updated = ?, error = 'Emergency stop: cancelled all tasks'
                WHERE status IN ('PENDING', 'RUNNING')
            """, (now,))
            return cur.rowcount


def get_unspoken_notifications() -> list[dict[str, Any]]:
    """Return COMPLETED or FAILED tasks that haven't been spoken to the user yet."""
    con = _conn()
    rows = con.execute("""
        SELECT * FROM tasks
        WHERE unspoken = 1 AND status IN ('COMPLETED', 'FAILED')
        ORDER BY updated ASC
    """).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        try:
            d["steps"] = json.loads(d.get("steps") or "[]")
        except Exception:
            d["steps"] = []
        results.append(d)
    return results


def mark_spoken(task_ids: list[str]) -> None:
    """Mark tasks as spoken so they won't be repeated."""
    if not task_ids:
        return
    con = _conn()
    placeholders = ",".join("?" * len(task_ids))
    with _write_lock:
        with con:
            con.execute(f"UPDATE tasks SET unspoken = 0 WHERE id IN ({placeholders})", task_ids)


def recover_stale_tasks() -> list[str]:
    """
    Called at app startup: any tasks left in 'RUNNING' status when the app exited
    are recovered. Mark them as FAILED ('Interrupted by app restart').
    Returns the list of recovered task IDs.
    """
    con = _conn()
    now = time.time()
    rows = con.execute("SELECT id FROM tasks WHERE status = 'RUNNING'").fetchall()
    recovered_ids = [r["id"] for r in rows]
    if recovered_ids:
        with _write_lock:
            with con:
                con.execute("""
                    UPDATE tasks
                    SET status = 'FAILED', updated = ?, error = 'Interrupted by application restart'
                    WHERE status = 'RUNNING'
                """, (now,))
    return recovered_ids
