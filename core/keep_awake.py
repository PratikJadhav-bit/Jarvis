"""
core/keep_awake.py — Windows Keep-Awake management (Phase 5).

Prevents the laptop from sleeping while background tasks are actively running.
Uses the Win32 SetThreadExecutionState API (zero new dependencies).

Flags:
  ES_CONTINUOUS:       0x80000000 — apply state until next continuous call
  ES_SYSTEM_REQUIRED: 0x00000001 — prevent system sleep
  ES_DISPLAY_REQUIRED:0x00000002 — keep monitor on (used for GUI/vision tasks)
"""
from __future__ import annotations

import ctypes
import logging
import sys
from contextlib import contextmanager
from typing import Generator

_log = logging.getLogger("keep_awake")

ES_CONTINUOUS       = 0x80000000
ES_SYSTEM_REQUIRED  = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

_active = False
_display_active = False


def acquire_keep_awake(display_required: bool = False) -> bool:
    """
    Prevent the system from entering idle sleep while tasks are running.
    If display_required=True, also keeps the display on (for GUI tasks).
    """
    global _active, _display_active
    if sys.platform != "win32":
        _active = True
        _display_active = display_required
        return True

    flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    if display_required:
        flags |= ES_DISPLAY_REQUIRED

    try:
        res = ctypes.windll.kernel32.SetThreadExecutionState(flags)
        if res != 0:
            _active = True
            _display_active = display_required
            _log.info(f"Keep-awake acquired (display={display_required})")
            return True
        else:
            _log.warning("SetThreadExecutionState returned 0")
            return False
    except Exception as e:
        _log.warning(f"Failed to acquire keep-awake: {e}")
        return False


def release_keep_awake() -> bool:
    """
    Release keep-awake when tasks are finished and system is idle.
    """
    global _active, _display_active
    if sys.platform != "win32":
        _active = False
        _display_active = False
        return True

    try:
        res = ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        _active = False
        _display_active = False
        _log.info("Keep-awake released")
        return res != 0
    except Exception as e:
        _log.warning(f"Failed to release keep-awake: {e}")
        return False


def is_keep_awake_active() -> bool:
    return _active


def is_display_keep_awake_active() -> bool:
    return _display_active


@contextmanager
def keep_awake_context(display_required: bool = False) -> Generator[None, None, None]:
    """Context manager for scoping keep-awake during task execution."""
    acquire_keep_awake(display_required)
    try:
        yield
    finally:
        release_keep_awake()
