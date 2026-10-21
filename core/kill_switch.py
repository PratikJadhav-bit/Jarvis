"""
core/kill_switch.py — Global emergency stop for Mark-LIII.

Two triggers:
  (a) Global hotkey  Ctrl+Alt+Shift+K  registered via ctypes RegisterHotKey
      on its own daemon thread.
  (b) Voice tool  emergency_stop  in actions/safety_tools.py (highest
      priority in prompt.txt).

Effect: sets policy.set_paused(True), cancels all running tasks (via the
tasks module when it exists), and calls pyautogui's failsafe.  The system
stays PAUSED until the user says "resume" or calls set_unattended(off).

No new dependencies: uses only ctypes (stdlib on Windows).
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import sys
import threading
from typing import Callable, Optional

from core import policy

# Windows hotkey constants
_MOD_ALT     = 0x0001
_MOD_CONTROL = 0x0002
_MOD_SHIFT   = 0x0004
_VK_K        = 0x4B    # 'K'
_WM_HOTKEY   = 0x0312
_HOTKEY_ID   = 9001    # arbitrary unique ID


_callbacks: list[Callable[[], None]] = []
_lock = threading.Lock()
_thread: Optional[threading.Thread] = None


def add_stop_callback(fn: Callable[[], None]) -> None:
    """Register a function to call when the kill switch fires."""
    with _lock:
        _callbacks.append(fn)


def _fire() -> None:
    """Called when the hotkey fires or emergency_stop tool is invoked."""
    policy.set_paused(True)

    # Cancel running tasks if the task system is loaded
    try:
        from core import task_queue
        task_queue.cancel_all_active_tasks()
    except Exception:
        pass

    # pyautogui failsafe: move cursor to (0,0)
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
    except Exception:
        pass

    with _lock:
        fns = list(_callbacks)
    for fn in fns:
        try:
            fn()
        except Exception:
            pass

    print("[KillSwitch] EMERGENCY STOP — all tasks cancelled, system PAUSED.", file=sys.stderr)


def trigger() -> None:
    """Programmatic trigger (e.g. from the voice tool)."""
    _fire()


def resume() -> None:
    """Release the paused state."""
    policy.set_paused(False)
    print("[KillSwitch] System RESUMED.", file=sys.stderr)


def is_paused() -> bool:
    """Check if emergency stop paused state is currently active."""
    return policy.is_paused()


def _hotkey_loop() -> None:
    """
    Daemon thread that registers Ctrl+Alt+Shift+K and pumps messages.
    Falls back silently if not on Windows or if registration fails.
    """
    if sys.platform != "win32":
        return

    user32 = ctypes.windll.user32
    ok = user32.RegisterHotKey(None, _HOTKEY_ID, _MOD_CONTROL | _MOD_ALT | _MOD_SHIFT, _VK_K)
    if not ok:
        # Another instance may have already registered it — not fatal
        print("[KillSwitch] Could not register Ctrl+Alt+Shift+K hotkey (code %d)."
              % ctypes.GetLastError(), file=sys.stderr)
        return

    msg = ctypes.wintypes.MSG()
    while True:
        ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
        if ret <= 0:
            break
        if msg.message == _WM_HOTKEY and msg.wParam == _HOTKEY_ID:
            _fire()

    user32.UnregisterHotKey(None, _HOTKEY_ID)


def start_hotkey_listener() -> None:
    """Start the hotkey listener thread once (idempotent)."""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        t = threading.Thread(target=_hotkey_loop, name="KillSwitch-Hotkey", daemon=True)
        t.start()
        _thread = t
