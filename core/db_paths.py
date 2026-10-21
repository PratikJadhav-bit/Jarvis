"""
core/db_paths.py — Safe database location resolution and OneDrive warning for Mark-LIII.

SQLite databases inside Microsoft OneDrive folders are vulnerable to:
  1. Sync locking conflicts (OneDrive locks .db, .db-wal, and .db-shm files).
  2. Latency spikes and disk I/O freezes during active WAL checkpoints.
  3. Database corruption if OneDrive replaces a database file while open.

This module ensures database locations are:
  - Fully configurable via `JARVIS_DATA_DIR` / `JARVIS_DB_DIR` env variables.
  - Automatically redirected outside OneDrive to %LOCALAPPDATA%\\Jarvis\\data
    (or ~/.jarvis/data) if the project is located within a OneDrive path.
  - Accompanied by a prominent startup warning when run inside OneDrive.
"""
from __future__ import annotations

import logging
import os
import platform
import sys
from pathlib import Path
from typing import Optional

_log = logging.getLogger("db_paths")


def get_project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def is_onedrive_path(path: Optional[Path] = None) -> bool:
    """Return True if the specified path (or project root) is inside OneDrive."""
    p = path or get_project_root()
    return "onedrive" in str(p.resolve()).lower()


def get_data_dir() -> Path:
    """
    Resolve the directory for SQLite databases:
      1. JARVIS_DATA_DIR or JARVIS_DB_DIR environment variable if specified.
      2. If project is inside OneDrive: redirect to %LOCALAPPDATA%\\Jarvis\\data (or ~/.jarvis/data).
      3. Otherwise: project_root / 'data'.
    """
    env_dir = os.environ.get("JARVIS_DATA_DIR") or os.environ.get("JARVIS_DB_DIR")
    if env_dir:
        target = Path(env_dir)
        target.mkdir(parents=True, exist_ok=True)
        return target

    root = get_project_root()
    if is_onedrive_path(root):
        if platform.system() == "Windows":
            local_appdata = os.environ.get("LOCALAPPDATA")
            if local_appdata:
                target = Path(local_appdata) / "Jarvis" / "data"
            else:
                target = Path.home() / ".jarvis" / "data"
        else:
            target = Path.home() / ".jarvis" / "data"
        target.mkdir(parents=True, exist_ok=True)
        return target

    target = root / "data"
    target.mkdir(parents=True, exist_ok=True)
    return target


def get_db_path(name: str) -> Path:
    """Return full path for a named database (e.g. 'tasks.db', 'audit.db', 'learning.db')."""
    return get_data_dir() / name


_WARNED_ONCE = False


def warn_if_onedrive() -> bool:
    """
    Check if the project is running inside a OneDrive directory.
    If so, print a prominent warning to stdout/stderr and log it.
    Returns True if a warning was emitted.
    """
    global _WARNED_ONCE
    if is_onedrive_path():
        root = get_project_root()
        data_dir = get_data_dir()
        msg = (
            f"\n"
            f"====================================================================\n"
            f"[Startup Warning] ⚠️  ONEDRIVE SYNC DETECTED:\n"
            f"Project root is inside a OneDrive synced folder:\n"
            f"  -> {root}\n"
            f"To prevent SQLite file-locking conflicts, WAL corruption, and sync\n"
            f"stalls, database storage has been redirected to safe local storage:\n"
            f"  -> {data_dir}\n"
            f"Override anytime by setting the JARVIS_DATA_DIR environment variable.\n"
            f"====================================================================\n"
        )
        if not _WARNED_ONCE:
            print(msg, file=sys.stderr)
            _log.warning(msg.strip())
            _WARNED_ONCE = True
        return True
    return False
