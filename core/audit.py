"""
core/audit.py — Append-only audit log for Mark-LIII.

All tool executions (policy decision + result) are recorded to a SQLite
database at memory/audit.db.  The schema is intentionally append-only:
there are no UPDATE or DELETE APIs exposed here so the log cannot be
tampered with by the agent.

Secrets are redacted before storage using a simple regex sweep that covers
common key/token patterns (64-char hex strings, "sk-…", "AIza…", etc.).
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from core import db_paths

_BASE = Path(__file__).resolve().parent.parent
_DB_PATH = db_paths.get_db_path("audit.db")
_db_lock = threading.Lock()


def set_db_path(path: Path) -> None:
    """Override database path (used by tests)."""
    global _DB_PATH, _conn
    close_connection()
    _DB_PATH = path


def close_connection() -> None:
    """Close active connection if open."""
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
        _conn = None

# ── Secret redaction ─────────────────────────────────────────────────────────

_SECRET_PATTERNS: list[re.Pattern] = [
    re.compile(r'\bAIza[A-Za-z0-9_\-]{35}\b'),           # Google API keys
    re.compile(r'\bsk-[A-Za-z0-9]{20,}\b'),               # OpenAI-style keys
    re.compile(r'\b[A-Za-z0-9]{64}\b'),                    # 64-char hex secrets
    re.compile(r'(?i)(password|token|secret|key)\s*[:=]\s*\S+'),  # key=value
    re.compile(r'Bearer\s+[A-Za-z0-9\-_\.]+'),            # Bearer tokens
]

_REDACT = "[REDACTED]"


def _redact(text: str) -> str:
    if not isinstance(text, str):
        return text
    for pat in _SECRET_PATTERNS:
        text = pat.sub(_REDACT, text)
    return text


def _redact_args(args: Any) -> str:
    """Serialize args to JSON string with secrets redacted."""
    try:
        raw = json.dumps(args, ensure_ascii=False, default=str)
    except Exception:
        raw = str(args)
    return _redact(raw)


# ── Schema init ───────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    task_id     TEXT,
    tool        TEXT    NOT NULL,
    args_json   TEXT,
    tier        TEXT    NOT NULL,
    decision    TEXT    NOT NULL,
    result_ok   INTEGER,
    output_summary TEXT
);
"""


def _get_conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.execute(_SCHEMA)
    conn.commit()
    return conn


_conn: Optional[sqlite3.Connection] = None


def _conn_singleton() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = _get_conn()
    return _conn


# ── Public API ────────────────────────────────────────────────────────────────

def log_decision(
    tool: str,
    args: Any,
    tier: str,
    decision: str,
    task_id: Optional[str] = None,
    result_ok: Optional[bool] = None,
    output_summary: Optional[str] = None,
) -> None:
    """
    Append one row to the audit log.  Never raises — a logging failure must
    not abort the action.
    """
    try:
        redacted_args    = _redact_args(args)
        redacted_summary = _redact(output_summary or "")
        with _db_lock:
            conn = _conn_singleton()
            conn.execute(
                """
                INSERT INTO audit_log
                    (ts, task_id, tool, args_json, tier, decision, result_ok, output_summary)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    time.time(),
                    task_id,
                    tool,
                    redacted_args,
                    tier,
                    decision,
                    int(result_ok) if result_ok is not None else None,
                    redacted_summary[:500] if redacted_summary else None,
                ),
            )
            conn.commit()
    except Exception as e:
        # Audit failures are non-fatal; print to stderr only
        import sys
        print(f"[Audit] log_decision failed: {e}", file=sys.stderr)


def audit_recent(n: int = 20) -> list[dict]:
    """
    Return the most recent ``n`` audit records as dicts (for the voice tool
    and the dashboard).  Safe to call from any thread.
    """
    try:
        with _db_lock:
            conn = _conn_singleton()
            rows = conn.execute(
                """
                SELECT ts, task_id, tool, tier, decision, result_ok, output_summary
                FROM   audit_log
                ORDER  BY id DESC
                LIMIT  ?
                """,
                (n,),
            ).fetchall()
        return [
            {
                "ts":             r[0],
                "task_id":        r[1],
                "tool":           r[2],
                "tier":           r[3],
                "decision":       r[4],
                "result_ok":      bool(r[5]) if r[5] is not None else None,
                "output_summary": r[6],
            }
            for r in rows
        ]
    except Exception:
        return []
