"""
actions/list_learned.py — Voice-facing tool to inspect learned workflows, recipes, macros, and preferences.
"""
from __future__ import annotations

from core import learning
from core.result import ActionResult, action_ok


def _list_learned(parameters: dict, **_) -> ActionResult:
    data = learning.list_learned()
    recipes = data.get("recipes", [])
    macros = data.get("macros", [])
    prefs = data.get("preferences", [])

    lines = [f"Learned database contains {len(recipes)} recipes, {len(macros)} macros, and {len(prefs)} user preferences."]
    if recipes:
        lines.append("Top recipes:")
        for r in recipes[:5]:
            lines.append(f"- {r['goal_pattern']} (success: {r['success_count']}, fail: {r['fail_count']})")
    if macros:
        lines.append("Macros:")
        for m in macros[:5]:
            lines.append(f"- {m['name']}: {m['goal_pattern']}")
    if prefs:
        lines.append("Preferences:")
        for p in prefs[:5]:
            lines.append(f"- {p['key']} = {p['value']}")

    return action_ok("\n".join(lines), **data)


TOOL = {
    "name": "list_learned",
    "description": (
        "List all learned workflows, recipes, macros, and user preferences stored by JARVIS."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {}
    },
    "handler": _list_learned
}
