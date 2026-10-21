"""
actions/task_status.py — Voice-facing tool to check task progress and outcome.
"""
from __future__ import annotations

import time
from core import task_queue
from core.result import ActionResult, action_ok, action_error


def _task_status(parameters: dict, **_) -> ActionResult:
    task_id = str(parameters.get("task_id") or "latest").strip()

    if task_id.lower() == "latest":
        task = task_queue.get_latest_task()
    else:
        task = task_queue.get_task(task_id)

    if not task:
        return action_error(f"No task found matching '{task_id}'.")

    status = task["status"]
    goal = task["goal"]
    created_str = time.strftime("%H:%M:%S", time.localtime(task["created"]))
    steps = task.get("steps") or []

    lines = [
        f"Task {task['id']}: {status}",
        f"Goal: {goal}",
        f"Created: {created_str}",
    ]

    if steps:
        lines.append(f"Steps completed: {len(steps)}")
        latest_step = steps[-1]
        lines.append(f"Last action: {latest_step.get('tool', 'unknown')} (success: {latest_step.get('success', False)})")

    if task.get("result"):
        lines.append(f"Result: {task['result']}")
    if task.get("error"):
        lines.append(f"Error: {task['error']}")

    task_data = {k: v for k, v in task.items() if k not in ("ok", "output", "error")}
    return action_ok("\n".join(lines), task=task_data)


TOOL = {
    "name": "task_status",
    "description": (
        "Check the status, current step, or result of a background task. "
        "Call when the user asks: how is my task going, what is task status, is the task done, "
        "or asks about the latest task."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "task_id": {
                "type": "STRING",
                "description": "The task ID to inspect, or 'latest' (default) for the most recently submitted task.",
            },
        },
        "required": [],
    },
    "handler": _task_status,
}
