"""
actions/unattended_tool.py — set_unattended voice tool for Mark-LIII.
"""
from __future__ import annotations

from core import policy
from core.result import action_ok


def _set_unattended(parameters: dict, **_) -> dict:
    on_str = str(parameters.get("enabled", "true")).strip().lower()
    on = on_str in ("true", "on", "1", "yes", "enable")
    policy.set_unattended(on)
    if on:
        return action_ok(
            "Unattended mode ON. I'll run safe operations automatically and "
            "queue anything risky for your approval."
        )
    return action_ok("Unattended mode OFF. I'll ask before every significant action.")


TOOL = {
    "name": "set_unattended",
    "description": (
        "Enable or disable unattended mode. When ON, JARVIS runs safe allowlisted "
        "operations automatically and queues everything else for your approval. "
        "When OFF (default), JARVIS asks before any significant action. "
        "Call when the user says: unattended mode on/off, run without asking, "
        "work in the background, stop asking me."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "enabled": {
                "type": "STRING",
                "description": "true | false",
            },
        },
        "required": ["enabled"],
    },
    "handler": _set_unattended,
}
