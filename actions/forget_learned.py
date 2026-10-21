"""
actions/forget_learned.py — Voice-facing tool to forget learned recipes, macros, commands, or preferences.
"""
from __future__ import annotations

from core import learning
from core.result import ActionResult, action_ok, action_error


def _forget_learned(parameters: dict, **_) -> ActionResult:
    query = str(parameters.get("query") or "").strip()
    if not query:
        return action_error("Please specify what learned item or topic to forget.")

    counts = learning.forget_learned(query)
    total = sum(counts.values())
    if total == 0:
        return action_ok(f"No learned recipes, macros, or preferences found matching '{query}'.")

    summary = (
        f"Forgot {total} item(s) matching '{query}' "
        f"({counts['recipes']} recipes, {counts['macros']} macros, "
        f"{counts['preferences']} preferences, {counts['commands']} commands)."
    )
    return action_ok(summary, query=query, counts=counts)


TOOL = {
    "name": "forget_learned",
    "description": (
        "Forget or delete learned execution recipes, macros, command logs, or preferences. "
        "Use when the user asks to forget how a task was done, delete a learned recipe, or clear a past pattern."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "query": {
                "type": "STRING",
                "description": "Keywords, app name, or goal pattern to forget (e.g. 'zip pdfs', 'chrome', 'report')."
            }
        },
        "required": ["query"]
    },
    "handler": _forget_learned
}
