"""
core/screen_lock.py — Detection of locked workstation and secure desktop (Phase 5).

Detects when the workstation is locked (Win+L), when a screensaver is running,
or when a UAC/secure desktop prompt is active.
Uses OpenInputDesktop from user32.dll (zero new dependencies).

Policy Rule:
  Never guess, type, or store the Windows login password.
  When the screen is locked:
    - GUI tasks (mouse/keyboard/vision/ui_automation) pause as `BLOCKED`.
    - Non-GUI tasks (scripts, powershell, file ops, web search) continue.
    - The user is notified via Telegram / Toast.
"""
from __future__ import annotations

import ctypes
import logging
import sys
from typing import Optional

_log = logging.getLogger("screen_lock")

# Mock hook for testing locked states
_mock_locked: Optional[bool] = None


def set_mock_screen_locked(locked: Optional[bool]) -> None:
    """Set mock locked state for testing."""
    global _mock_locked
    _mock_locked = locked


def is_screen_locked() -> bool:
    """
    Returns True if the workstation is locked, in screensaver, or on secure desktop.
    Returns False if the interactive desktop is accessible.
    """
    global _mock_locked
    if _mock_locked is not None:
        return _mock_locked

    if sys.platform != "win32":
        return False

    try:
        # DESKTOP_READOBJECTS = 0x0001
        hdesk = ctypes.windll.user32.OpenInputDesktop(0, False, 0x0001)
        if not hdesk:
            # OpenInputDesktop fails with ERROR_ACCESS_DENIED (5) when desktop is locked
            return True
        ctypes.windll.user32.CloseDesktop(hdesk)
        return False
    except Exception as e:
        _log.warning(f"Error checking desktop lock status: {e}")
        return False


def is_gui_action(tool: str, args: Optional[dict] = None) -> bool:
    """Check if an action requires an active, unlocked display."""
    gui_tools = {
        "ui_automation",
        "universal_control",
        "desktop",
        "computer_control",
        "screen_process",
    }
    if tool in gui_tools:
        return True
    return False
