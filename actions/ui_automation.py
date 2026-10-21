"""
actions/ui_automation.py — Windows UI Automation via pywinauto for native apps.

Provides tier-2 control (before falling back to computer vision / screenshot clicking).
Inspects, focuses, types into, and clicks native Windows UI elements.
"""
from __future__ import annotations

import sys
from typing import Optional

from core.result import ActionResult, action_ok, action_error

try:
    import pywinauto
    from pywinauto import Desktop
    _HAS_PYWINAUTO = True
except ImportError:
    _HAS_PYWINAUTO = False


def _ui_automation(parameters: dict, **_) -> ActionResult:
    if not _HAS_PYWINAUTO:
        return action_error("pywinauto is not installed in the environment.")

    action = (parameters.get("action") or "list_windows").strip().lower()
    title = parameters.get("title", "")
    control = parameters.get("control", "")
    text = parameters.get("text", "")

    try:
        desktop = Desktop(backend="uia")

        if action == "list_windows":
            windows = desktop.windows()
            visible = []
            for w in windows:
                try:
                    t = w.window_text().strip()
                    if t and w.is_visible():
                        visible.append(t)
                except Exception:
                    continue
            summary = f"Active windows ({len(visible)}):\n" + "\n".join(f"• {t}" for t in visible[:20])
            return action_ok(summary, windows=visible)

        if action == "focus":
            if not title:
                return action_error("Missing 'title' parameter to focus window.")
            win = desktop.window(title_re=f".*{title}.*", visible_only=True)
            win.set_focus()
            return action_ok(f"Focused window matching '{title}'.")

        if action == "click":
            if not title:
                return action_error("Missing 'title' parameter.")
            win = desktop.window(title_re=f".*{title}.*", visible_only=True)
            if control:
                ctrl = win.child_window(title_re=f".*{control}.*", control_type="Button")
                ctrl.click_input()
                return action_ok(f"Clicked button '{control}' in window '{title}'.")
            else:
                win.click_input()
                return action_ok(f"Clicked window '{title}'.")

        if action == "type_keys":
            if not title:
                return action_error("Missing 'title' parameter.")
            win = desktop.window(title_re=f".*{title}.*", visible_only=True)
            win.set_focus()
            if control:
                ctrl = win.child_window(title_re=f".*{control}.*", control_type="Edit")
                ctrl.type_keys(text, with_spaces=True)
            else:
                win.type_keys(text, with_spaces=True)
            return action_ok(f"Typed into window '{title}'.")

        if action == "get_text":
            if not title:
                return action_error("Missing 'title' parameter.")
            win = desktop.window(title_re=f".*{title}.*", visible_only=True)
            texts = win.texts()
            flat_texts = [t for sub in texts for t in (sub if isinstance(sub, list) else [sub]) if t.strip()]
            return action_ok("\n".join(flat_texts[:30]), texts=flat_texts)

        if action == "close":
            if not title:
                return action_error("Missing 'title' parameter.")
            win = desktop.window(title_re=f".*{title}.*", visible_only=True)
            win.close()
            return action_ok(f"Closed window matching '{title}'.")

        return action_error(f"Unknown ui_automation action: '{action}'.")

    except Exception as e:
        return action_error(f"UI Automation failed: {e}")


TOOL = {
    "name": "ui_automation",
    "description": (
        "Interact with native Windows desktop applications using Windows UI Automation (pywinauto). "
        "Allows finding, focusing, clicking buttons, typing text, reading UI control text, and closing windows. "
        "Use this for native apps before falling back to screenshot/vision clicking. "
        "action: list_windows | focus | click | type_keys | get_text | close"
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "list_windows (default) | focus | click | type_keys | get_text | close",
            },
            "title": {
                "type": "STRING",
                "description": "Title or partial title of the window to target.",
            },
            "control": {
                "type": "STRING",
                "description": "Button label or control text to target inside the window.",
            },
            "text": {
                "type": "STRING",
                "description": "Text to type when action='type_keys'.",
            },
        },
        "required": [],
    },
    "handler": _ui_automation,
}
