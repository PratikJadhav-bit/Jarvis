"""
core/learning.py — Learning from user commands and task executions (Phase 4).

Features:
  - SQLite storage in memory/learning.db:
      commands: automatic command logging (raw_text, interpreted_goal, task_id)
      runs: outcome, duration, steps count, tools used
      feedback: user praise (+1), corrections (-1), silence (weak positive)
      recipes: distilled execution recipes (goal pattern, steps, gotchas, success/fail counts)
      preferences: durable extracted user preferences
      macros: named workflows with parameter slots
  - FTS5 virtual table for fast similarity retrieval.
  - Ranking by similarity * success_rate * recency.
  - Automatic secret redaction (no passwords, tokens, or screenshots stored).
  - Oldest-first pruning for retention control.
  - Voice tools: forget_learned(query), list_learned().
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from core import db_paths
from core.audit import _redact

_log = logging.getLogger("learning")

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

_BASE = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = db_paths.get_db_path("learning.db")
_db_path: Path = _DEFAULT_DB_PATH

_local = threading.local()
_open_connections: list[sqlite3.Connection] = []
_conn_lock = threading.Lock()

MAX_COMMANDS_RETENTION = 1000
MAX_RUNS_RETENTION = 1000


def set_db_path(path: Path) -> None:
    """Set database path (used by unit tests)."""
    global _db_path
    close_all_connections()
    _db_path = path


def close_all_connections() -> None:
    """Close all open connections across all threads."""
    with _conn_lock:
        for con in list(_open_connections):
            try:
                con.close()
            except Exception:
                pass
        _open_connections.clear()
        if hasattr(_local, "con"):
            _local.con = None


def _get_connection() -> sqlite3.Connection:
    con = getattr(_local, "con", None)
    if con is None:
        _db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(_db_path), timeout=10.0)
        con.row_factory = sqlite3.Row
        with con:
            con.execute("PRAGMA journal_mode=WAL;")
            con.execute("PRAGMA busy_timeout=5000;")
        _local.con = con
        with _conn_lock:
            _open_connections.append(con)
        _init_db(con)
    return con


def _init_db(con: sqlite3.Connection) -> None:
    with con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS commands (
            id                  TEXT PRIMARY KEY,
            ts                  TEXT NOT NULL,
            raw_text            TEXT NOT NULL,
            interpreted_goal    TEXT NOT NULL,
            task_id             TEXT
        );
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            task_id             TEXT PRIMARY KEY,
            goal                TEXT,
            outcome             TEXT NOT NULL,
            duration            REAL NOT NULL,
            steps               INTEGER NOT NULL,
            tools               TEXT NOT NULL,
            ts                  TEXT NOT NULL
        );
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            task_id             TEXT NOT NULL,
            signal              TEXT NOT NULL,
            ts                  TEXT NOT NULL
        );
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS recipes (
            id                  TEXT PRIMARY KEY,
            goal_pattern        TEXT NOT NULL,
            steps_json          TEXT NOT NULL,
            preconditions       TEXT,
            gotchas             TEXT,
            success_count       INTEGER DEFAULT 1,
            fail_count          INTEGER DEFAULT 0,
            last_used           TEXT NOT NULL
        );
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS preferences (
            id                  TEXT PRIMARY KEY,
            key                 TEXT NOT NULL UNIQUE,
            value               TEXT NOT NULL,
            source              TEXT,
            ts                  TEXT NOT NULL
        );
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS macros (
            id                  TEXT PRIMARY KEY,
            name                TEXT NOT NULL UNIQUE,
            goal_pattern        TEXT NOT NULL,
            steps_json          TEXT NOT NULL,
            params_json         TEXT,
            created_ts          TEXT NOT NULL
        );
        """)

        # FTS5 virtual table for recipes
        try:
            con.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS recipes_fts USING fts5(
                id UNINDEXED,
                goal_pattern,
                steps_json,
                preconditions,
                gotchas
            );
            """)
        except sqlite3.OperationalError:
            # Fallback if FTS5 is compiled out in minimal environments
            _log.warning("FTS5 table creation failed, keyword fallback will be used.")


# ── Command Logging ──────────────────────────────────────────────────────────

def _prune_retention(con: sqlite3.Connection) -> None:
    """Keep table sizes within bounds by pruning oldest rows first."""
    try:
        with con:
            cmd_count = con.execute("SELECT COUNT(*) FROM commands").fetchone()[0]
            if cmd_count > MAX_COMMANDS_RETENTION:
                drop_n = cmd_count - MAX_COMMANDS_RETENTION
                con.execute("""
                DELETE FROM commands WHERE id IN (
                    SELECT id FROM commands ORDER BY ts ASC LIMIT ?
                )
                """, (drop_n,))

            run_count = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            if run_count > MAX_RUNS_RETENTION:
                drop_n = run_count - MAX_RUNS_RETENTION
                con.execute("""
                DELETE FROM runs WHERE task_id IN (
                    SELECT task_id FROM runs ORDER BY ts ASC LIMIT ?
                )
                """, (drop_n,))
    except Exception as e:
        _log.warning(f"Error pruning learning DB retention: {e}")


def log_command(raw_text: str, interpreted_goal: str, task_id: Optional[str] = None) -> str:
    """
    Log every spoken or submitted command automatically.
    Redacts any sensitive tokens and stores text only.
    """
    cmd_id = f"cmd_{uuid.uuid4().hex[:12]}"
    ts = _utc_now_iso()
    clean_raw = _redact(str(raw_text or "").strip())
    clean_goal = _redact(str(interpreted_goal or clean_raw).strip())

    con = _get_connection()
    with con:
        con.execute("""
        INSERT INTO commands (id, ts, raw_text, interpreted_goal, task_id)
        VALUES (?, ?, ?, ?, ?)
        """, (cmd_id, ts, clean_raw, clean_goal, task_id))

    _prune_retention(con)
    return cmd_id


# ── Normalization & Pattern Helpers ──────────────────────────────────────────

def normalize_goal(goal: str) -> str:
    """Normalize goal for matching: lowercase, strip punctuation, clean whitespace."""
    g = goal.lower()
    g = re.sub(r'[^\w\s]', ' ', g)
    return re.sub(r'\s+', ' ', g).strip()


def _sanitize_steps(steps: list[dict]) -> list[dict]:
    """Clean step dictionaries for recipe storage, removing ephemeral states and redacting secrets."""
    clean = []
    for s in steps:
        tool = s.get("tool") or s.get("action")
        args = s.get("args") or {}
        if not tool:
            continue
        # Redact any strings in args
        redacted_args = json.loads(_redact(json.dumps(args, default=str)))
        clean.append({
            "tool": tool,
            "args": redacted_args,
            "description": s.get("description", f"Run {tool}")
        })
    return clean


# ── Run Recording & Recipe Distillation ──────────────────────────────────────

def record_run(
    task_id: str,
    goal: str,
    outcome: str,
    duration: float,
    steps: list[dict],
    tools: list[str]
) -> None:
    """
    Record task outcome, duration, and steps into runs table.
    On success, distills the run into a reusable recipe.
    """
    ts = _utc_now_iso()
    clean_goal = _redact(goal.strip())
    tools_str = json.dumps(tools)

    con = _get_connection()
    with con:
        con.execute("""
        INSERT OR REPLACE INTO runs (task_id, goal, outcome, duration, steps, tools, ts)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (task_id, clean_goal, outcome.lower(), duration, len(steps), tools_str, ts))

    # Extract durable preferences if applicable
    try:
        extract_preferences_from_run(clean_goal, steps)
    except Exception as e:
        _log.warning(f"Preference extraction error: {e}")

    # Distill recipe on success
    if outcome.lower() == "success" and steps:
        distill_recipe(clean_goal, steps)


def distill_recipe(goal: str, steps: list[dict]) -> str:
    """
    Compress a successful run into a parameterized recipe.
    Extracts gotchas if any steps failed and were subsequently self-corrected.
    """
    pattern = normalize_goal(goal)
    clean_steps = _sanitize_steps(steps)
    steps_json = json.dumps(clean_steps)
    ts = _utc_now_iso()

    # Detect self-corrections / gotchas in the step history
    gotchas_list = []
    for i, s in enumerate(steps):
        if not s.get("ok", True):
            tool = s.get("tool")
            err = s.get("error", "Failed")
            # See if later step succeeded for same tool or alternative
            for s_next in steps[i + 1:]:
                if s_next.get("ok", True):
                    gotchas_list.append(f"Failed {tool} with '{err}' -> resolved by {s_next.get('tool')}")
                    break
    gotchas = "; ".join(gotchas_list) if gotchas_list else ""

    con = _get_connection()
    with con:
        # Check if an existing recipe matches this goal pattern
        row = con.execute("SELECT id, success_count, fail_count FROM recipes WHERE goal_pattern = ?", (pattern,)).fetchone()
        if row:
            rec_id = row["id"]
            succ = row["success_count"] + 1
            con.execute("""
            UPDATE recipes
            SET success_count = ?, steps_json = ?, gotchas = ?, last_used = ?
            WHERE id = ?
            """, (succ, steps_json, gotchas, ts, rec_id))

            # Update FTS5
            try:
                con.execute("DELETE FROM recipes_fts WHERE id = ?", (rec_id,))
                con.execute("""
                INSERT INTO recipes_fts (id, goal_pattern, steps_json, preconditions, gotchas)
                VALUES (?, ?, ?, ?, ?)
                """, (rec_id, pattern, steps_json, "", gotchas))
            except Exception:
                pass

            # Check if candidate for automatic macro suggestion (3+ successes)
            if succ >= 3:
                _maybe_suggest_macro(pattern, clean_steps)

            return rec_id
        else:
            rec_id = f"rec_{uuid.uuid4().hex[:12]}"
            con.execute("""
            INSERT INTO recipes (id, goal_pattern, steps_json, preconditions, gotchas, success_count, fail_count, last_used)
            VALUES (?, ?, ?, ?, ?, 1, 0, ?)
            """, (rec_id, pattern, steps_json, "", gotchas, ts))

            # Insert into FTS5
            try:
                con.execute("""
                INSERT INTO recipes_fts (id, goal_pattern, steps_json, preconditions, gotchas)
                VALUES (?, ?, ?, ?, ?)
                """, (rec_id, pattern, steps_json, "", gotchas))
            except Exception:
                pass

            return rec_id


# ── Recipe Retrieval & Replay ────────────────────────────────────────────────

def retrieve_recipes(goal: str, top_k: int = 5) -> list[dict[str, Any]]:
    """
    Fetch top relevant recipes ranked by: similarity * (success_rate) * recency.
    Returns structured list of recipe dicts.
    """
    con = _get_connection()
    pattern = normalize_goal(goal)
    if not pattern:
        return []

    # Try FTS5 first
    candidate_ids = []
    # Build clean query tokens
    tokens = [re.sub(r'[^a-zA-Z0-9]', '', t) for t in pattern.split() if len(t) > 2]
    fts_query = " OR ".join(tokens) if tokens else pattern

    try:
        cur = con.execute("SELECT id FROM recipes_fts WHERE recipes_fts MATCH ? LIMIT 20", (fts_query,))
        candidate_ids = [r[0] for r in cur.fetchall()]
    except Exception:
        pass

    # If FTS returned nothing or wasn't available, search via LIKE
    if not candidate_ids:
        cur = con.execute("SELECT id FROM recipes WHERE goal_pattern LIKE ? LIMIT 20", (f"%{pattern[:20]}%",))
        candidate_ids = [r[0] for r in cur.fetchall()]

    if not candidate_ids:
        # Fallback to recent recipes
        cur = con.execute("SELECT id FROM recipes ORDER BY last_used DESC LIMIT 10")
        candidate_ids = [r[0] for r in cur.fetchall()]

    if not candidate_ids:
        return []

    placeholders = ",".join("?" for _ in candidate_ids)
    rows = con.execute(f"SELECT * FROM recipes WHERE id IN ({placeholders})", candidate_ids).fetchall()

    scored = []
    goal_words = set(pattern.split())

    for r in rows:
        rec_pattern = r["goal_pattern"]
        rec_words = set(rec_pattern.split())
        # Word overlap Jaccard similarity
        overlap = len(goal_words & rec_words)
        union = len(goal_words | rec_words) or 1
        similarity = overlap / union

        # Exact substring boost
        if rec_pattern in pattern or pattern in rec_pattern:
            similarity = max(similarity, 0.85)
        if rec_pattern == pattern:
            similarity = 1.0

        succ = r["success_count"]
        fail = r["fail_count"]
        success_rate = (succ + 1) / (succ + fail + 1)

        # Recency score
        recency = 1.0
        try:
            dt = datetime.fromisoformat(r["last_used"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
            recency = 1.0 / (1.0 + age_days * 0.1)
        except Exception:
            pass

        rank_score = similarity * success_rate * recency
        scored.append((rank_score, dict(r)))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [item[1] for item in scored[:top_k] if item[0] > 0.1]


def get_replay_plan(goal: str) -> Optional[list[dict]]:
    """
    If a top recipe is a strong match (>= 0.8) and has no recent failures,
    returns its steps for fast verified replay.
    """
    recipes = retrieve_recipes(goal, top_k=1)
    if not recipes:
        return None
    top = recipes[0]
    pattern = normalize_goal(goal)
    rec_pattern = top["goal_pattern"]

    # Only replay if strong pattern match and no failure dominance
    if rec_pattern == pattern or rec_pattern in pattern or pattern in rec_pattern:
        if top.get("fail_count", 0) <= top.get("success_count", 0):
            try:
                steps = json.loads(top["steps_json"])
                if isinstance(steps, list) and len(steps) > 0:
                    return steps
            except Exception:
                pass
    return None


# ── Feedback Signals ─────────────────────────────────────────────────────────

def record_feedback(task_id: str, signal: str) -> bool:
    """
    Record user feedback (+1, -1, weak positive).
    Adjusts the success/fail count of any recipe associated with the task.
    """
    ts = _utc_now_iso()
    signal = signal.lower().strip()
    con = _get_connection()

    with con:
        con.execute("INSERT INTO feedback (task_id, signal, ts) VALUES (?, ?, ?)", (task_id, signal, ts))

        # Find run to locate goal and recipe
        run = con.execute("SELECT goal FROM runs WHERE task_id = ?", (task_id,)).fetchone()
        if run and run["goal"]:
            pattern = normalize_goal(run["goal"])
            rec = con.execute("SELECT id, success_count, fail_count FROM recipes WHERE goal_pattern = ?", (pattern,)).fetchone()
            if rec:
                if signal in ("negative", "-1", "bad", "wrong"):
                    con.execute("UPDATE recipes SET fail_count = fail_count + 1 WHERE id = ?", (rec["id"],))
                elif signal in ("positive", "+1", "good", "praise"):
                    con.execute("UPDATE recipes SET success_count = success_count + 1 WHERE id = ?", (rec["id"],))

    return True


_NEGATIVE_WORDS = {"no", "not that", "wrong", "undo", "stop that", "cancel that", "bad"}
_POSITIVE_WORDS = {"good", "great", "thanks", "perfect", "well done", "awesome", "nice job"}


def detect_spoken_feedback(
    spoken_text: str,
    last_completed_task_id: Optional[str],
    last_completed_ts: float,
    current_time: Optional[float] = None
) -> Optional[tuple[str, str]]:
    """
    Detect user feedback in spoken text within 60 s of task completion.
    Returns (signal, task_id) or None.
    """
    if not last_completed_task_id or not last_completed_ts:
        return None

    now = current_time if current_time is not None else time.time()
    if (now - last_completed_ts) > 60.0:
        return None

    clean = spoken_text.lower().strip()
    for neg in _NEGATIVE_WORDS:
        if neg in clean:
            return ("negative", last_completed_task_id)

    for pos in _POSITIVE_WORDS:
        if pos in clean:
            return ("positive", last_completed_task_id)

    return None


# ── Preferences Extraction ───────────────────────────────────────────────────

_BROWSER_KEYWORDS = ("chrome", "firefox", "edge", "brave", "safari")
_FOLDER_PATTERNS = re.compile(r'([A-Za-z]:\\[\w\\\s\.\-]+|~/[\w/\.\-]+|downloads|desktop|documents)', re.IGNORECASE)


def extract_preferences_from_run(goal: str, steps: list[dict]) -> None:
    """
    Extract durable preferences from goal/steps and update:
    1. Local learning preferences table
    2. memory.memory_manager (so it shows in HUD Memory Panel)
    """
    con = _get_connection()
    ts = _utc_now_iso()
    clean_goal = goal.lower()

    # Browser preference
    for b in _BROWSER_KEYWORDS:
        if b in clean_goal:
            pref_key = "default_browser"
            pref_val = b
            with con:
                con.execute("""
                INSERT OR REPLACE INTO preferences (id, key, value, source, ts)
                VALUES (?, ?, ?, ?, ?)
                """, (f"pref_{pref_key}", pref_key, pref_val, "run_inference", ts))
            # Sync to memory manager
            try:
                from memory import memory_manager
                memory_manager.remember(pref_key, pref_val, category="preferences")
            except Exception:
                pass
            break

    # Directory / folder preferences
    folder_match = _FOLDER_PATTERNS.search(goal)
    if folder_match:
        folder = folder_match.group(1).strip()
        pref_key = "preferred_folder"
        with con:
            con.execute("""
            INSERT OR REPLACE INTO preferences (id, key, value, source, ts)
            VALUES (?, ?, ?, ?, ?)
            """, (f"pref_{pref_key}", pref_key, folder, "run_inference", ts))
        try:
            from memory import memory_manager
            memory_manager.remember(pref_key, folder, category="preferences")
        except Exception:
            pass


# ── Macros ───────────────────────────────────────────────────────────────────

def _maybe_suggest_macro(goal_pattern: str, steps: list[dict]) -> None:
    """Internal helper: automatically register macro proposal for 3+ repeat patterns."""
    name = f"macro_{goal_pattern.replace(' ', '_')[:30]}"
    try:
        save_macro(name=name, goal_pattern=goal_pattern, steps=steps)
    except Exception:
        pass


def save_macro(name: str, goal_pattern: str, steps: list[dict], params: Optional[dict] = None) -> str:
    """Save a named macro workflow."""
    con = _get_connection()
    macro_id = f"mac_{uuid.uuid4().hex[:12]}"
    ts = _utc_now_iso()
    steps_json = json.dumps(_sanitize_steps(steps))
    params_json = json.dumps(params or {})

    with con:
        con.execute("""
        INSERT OR REPLACE INTO macros (id, name, goal_pattern, steps_json, params_json, created_ts)
        VALUES (?, ?, ?, ?, ?, ?)
        """, (macro_id, name, goal_pattern, steps_json, params_json, ts))
    return macro_id


def list_macros() -> list[dict]:
    con = _get_connection()
    rows = con.execute("SELECT * FROM macros ORDER BY created_ts DESC").fetchall()
    return [dict(r) for r in rows]


def get_macro(name_or_id: str) -> Optional[dict]:
    con = _get_connection()
    row = con.execute("SELECT * FROM macros WHERE id = ? OR name = ?", (name_or_id, name_or_id)).fetchone()
    return dict(row) if row else None


# ── Control & Forgetting ─────────────────────────────────────────────────────

def forget_learned(query: str) -> dict[str, int]:
    """
    Search and delete matching recipes, macros, commands, and preferences.
    Never overrides safety policy tiers.
    """
    con = _get_connection()
    pattern = f"%{query.strip().lower()}%"
    counts = {"recipes": 0, "macros": 0, "commands": 0, "preferences": 0}

    with con:
        # Delete recipes
        recs = con.execute("SELECT id FROM recipes WHERE goal_pattern LIKE ?", (pattern,)).fetchall()
        for r in recs:
            con.execute("DELETE FROM recipes WHERE id = ?", (r["id"],))
            try:
                con.execute("DELETE FROM recipes_fts WHERE id = ?", (r["id"],))
            except Exception:
                pass
            counts["recipes"] += 1

        # Delete macros
        macs = con.execute("SELECT id FROM macros WHERE name LIKE ? OR goal_pattern LIKE ?", (pattern, pattern)).fetchall()
        for m in macs:
            con.execute("DELETE FROM macros WHERE id = ?", (m["id"],))
            counts["macros"] += 1

        # Delete commands
        cmds = con.execute("SELECT id FROM commands WHERE raw_text LIKE ? OR interpreted_goal LIKE ?", (pattern, pattern)).fetchall()
        for c in cmds:
            con.execute("DELETE FROM commands WHERE id = ?", (c["id"],))
            counts["commands"] += 1

        # Delete preferences
        prefs = con.execute("SELECT key FROM preferences WHERE key LIKE ? OR value LIKE ?", (pattern, pattern)).fetchall()
        for p in prefs:
            con.execute("DELETE FROM preferences WHERE key = ?", (p["key"],))
            counts["preferences"] += 1
            # Also forget from memory_manager
            try:
                from memory import memory_manager
                memory_manager.forget(p["key"], category="preferences")
            except Exception:
                pass

    return counts


def list_learned() -> dict[str, Any]:
    """Return overview of learned recipes, macros, and preferences."""
    con = _get_connection()
    recs = [dict(r) for r in con.execute("SELECT id, goal_pattern, success_count, fail_count, last_used FROM recipes ORDER BY success_count DESC LIMIT 20").fetchall()]
    macs = [dict(r) for r in con.execute("SELECT id, name, goal_pattern FROM macros ORDER BY created_ts DESC LIMIT 20").fetchall()]
    prefs = [dict(r) for r in con.execute("SELECT key, value, ts FROM preferences ORDER BY ts DESC LIMIT 20").fetchall()]

    return {
        "recipes": recs,
        "macros": macs,
        "preferences": prefs
    }
