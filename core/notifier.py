"""
core/notifier.py — Windows Toast notifications and spoken summaries for Mark-LIII.

Features:
  - Windows Toast: Uses win10toast if available on a background thread,
    or falls back to PowerShell WinRT toast notification or logging.
  - Spoken summaries: Dispatches concise messages to the user via
    main.py's speech channel or queued spoken summary.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
from typing import Callable, Optional

_log = logging.getLogger("notifier")


def _toast_worker(title: str, message: str) -> None:
    # 1. Try win10toast
    try:
        from win10toast import ToastNotifier
        toaster = ToastNotifier()
        toaster.show_toast(
            title=title[:60],
            msg=message[:120],
            duration=5,
            threaded=False  # already on daemon thread
        )
        return
    except Exception as e:
        _log.debug(f"win10toast failed: {e}")

    # 2. Fallback: PowerShell BurntToast / WinRT toast
    try:
        ps_cmd = (
            f"[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null; "
            f"$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
            f"$textNodes = $template.GetElementsByTagName('text'); "
            f"$textNodes.Item(0).AppendChild($template.CreateTextNode('{title[:50]}')) > $null; "
            f"$textNodes.Item(1).AppendChild($template.CreateTextNode('{message[:100]}')) > $null; "
            f"$notification = [Windows.UI.Notifications.ToastNotification]::new($template); "
            f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('JARVIS Assistant').Show($notification);"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd],
                       capture_output=True, timeout=5)
    except Exception as e:
        _log.debug(f"PowerShell toast failed: {e}")


def send_toast(title: str, message: str) -> None:
    """Send a native Windows toast notification without blocking the caller."""
    t = threading.Thread(target=_toast_worker, args=(title, message), daemon=True)
    t.start()


def notify_task_completion(
    task_id: str,
    goal: str,
    status: str,
    result: Optional[str] = None,
    error: Optional[str] = None,
    say_fn: Optional[Callable[[str], None]] = None,
) -> None:
    """
    Called when a background task finishes (COMPLETED or FAILED).
    Triggers a Windows toast, and speaks immediately if say_fn is available and active.
    """
    is_ok = (status == "COMPLETED")
    title = f"JARVIS: Task {'Completed' if is_ok else 'Failed'}"
    summary = result if is_ok else (error or "Task failed")
    body = f"{goal[:60]} — {summary[:80]}"

    # Send Windows toast
    send_toast(title, body)

    # If active speech channel is provided, announce it
    if say_fn:
        spoken_msg = (
            f"Sir, your background task '{goal[:40]}' has completed: {summary[:100]}"
            if is_ok else
            f"Sir, your background task '{goal[:40]}' encountered an issue: {summary[:100]}"
        )
        try:
            say_fn(spoken_msg)
        except Exception as e:
            _log.debug(f"say_fn failed: {e}")
