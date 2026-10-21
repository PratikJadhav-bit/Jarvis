"""
actions/audit_tool.py — audit_recent voice tool for Mark-LIII.
"""
from __future__ import annotations

import time as _time

from core.audit import audit_recent as _fetch_audit
from core.result import action_ok


def _audit_recent(parameters: dict, **_) -> dict:
    try:
        n = int(parameters.get("count", 10))
        n = max(1, min(n, 50))
    except (TypeError, ValueError):
        n = 10

    records = _fetch_audit(n)
    if not records:
        return action_ok("The audit log is empty.")

    lines = []
    for r in records:
        ts   = _time.strftime("%H:%M:%S", _time.localtime(r["ts"]))
        ok   = "✓" if r.get("result_ok") else ("✗" if r.get("result_ok") is False else "–")
        summ = (r.get("output_summary") or "")[:60]
        lines.append(f"[{ts}] {r['tool']} ({r['tier']}/{r['decision']}) {ok} {summ}")

    return action_ok("\n".join(lines))


TOOL = {
    "name": "audit_recent",
    "description": (
        "Show recent audit log entries: every tool call with its safety tier, "
        "approval decision, and outcome. Call when the user asks: what did you do, "
        "show audit log, what happened, what tools were run."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "count": {
                "type": "STRING",
                "description": "Number of recent entries to show (default 10, max 50).",
            },
        },
        "required": [],
    },
    "handler": _audit_recent,
}
