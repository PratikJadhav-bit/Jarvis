"""
actions/safety_tools.py — Safety & control voice tools for Mark-LIII.

Exposes three TOOL dicts auto-discovered by core/action_loader:
  emergency_stop  — "stop everything" kill switch (top-priority)
  set_unattended  — enable/disable unattended mode
  audit_recent    — fetch recent audit log entries
"""
from __future__ import annotations

from core import policy
from core import kill_switch
from core.audit import audit_recent as _fetch_audit
from core.result import action_ok, action_error

# ── emergency_stop ────────────────────────────────────────────────────────────

def _emergency_stop(parameters: dict, **_) -> dict:
    action = (parameters.get("action") or "stop").strip().lower()

    if action in ("stop", "halt", "cancel", "pause"):
        kill_switch.trigger()
        return action_ok("Emergency stop activated. All tasks cancelled. Say 'resume' to continue.")

    if action == "resume":
        kill_switch.resume()
        return action_ok("System resumed. I'm listening again.")

    if action == "status":
        state = "PAUSED" if policy.is_paused() else "ACTIVE"
        return action_ok(f"System status: {state}.")

    return action_error(f"Unknown emergency_stop action: {action!r}. Use stop, resume, or status.")


TOOL = {
    "name": "emergency_stop",
    "description": (
        "HIGHEST PRIORITY TOOL. Stop, cancel, or halt everything immediately. "
        "Call when the user says: stop, halt, stop everything, cancel all, emergency stop, abort, "
        "pause everything, or resume (to restart after a stop). "
        "Also call if anything seems wrong or out of control. "
        "Use action='resume' to restart after a stop, action='status' to check state."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "stop (default) | resume | status",
            },
        },
        "required": [],
    },
    "handler": _emergency_stop,
}
