"""
actions/list_tasks.py — Voice-facing tool to list background tasks.
"""
from __future__ import annotations

import time
from core import task_queue
from core.result import ActionResult, action_ok


def _list_tasks(parameters: dict, **_) -> ActionResult:
    status_filter = parameters.get("status")
    try:
        limit = int(parameters.get("limit", 5))
        limit = max(1, min(limit, 20))
    except (TypeError, ValueError):
        limit = 5

    tasks = task_queue.list_tasks(status=status_filter, limit=limit)
    if not tasks:
        msg = f"No background tasks found{' matching ' + status_filter if status_filter else ''}."
        return action_ok(msg, count=0, tasks=[])

    lines = [f"Found {len(tasks)} recent task(s):"]
    for t in tasks:
        created_str = time.strftime("%H:%M:%S", time.localtime(t["created"]))
        lines.append(f"• [{t['id']}] {t['status']}: {t['goal'][:50]} (started {created_str})")

    return action_ok("\n".join(lines), count=len(tasks), tasks=tasks)


TOOL = {
    "name": "list_tasks",
    "description": (
        "List recent background tasks and their statuses (PENDING, RUNNING, COMPLETED, FAILED, CANCELLED). "
        "Call when user asks: what tasks are running, show my tasks, list tasks, background task history."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "status": {
                "type": "STRING",
                "description": "Optional status filter: all (default) | PENDING | RUNNING | COMPLETED | FAILED | CANCELLED",
            },
            "limit": {
                "type": "INTEGER",
                "description": "Maximum number of tasks to return (default 5, max 20).",
            },
        },
        "required": [],
    },
    "handler": _list_tasks,
}
