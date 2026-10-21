"""
actions/cancel_task.py — Voice-facing tool to cancel a running or pending task.
"""
from __future__ import annotations

from core import task_queue
from core.result import ActionResult, action_ok, action_error


def _cancel_task(parameters: dict, **_) -> ActionResult:
    task_id = str(parameters.get("task_id") or "latest").strip()

    if task_id.lower() == "latest":
        latest = task_queue.get_latest_task()
        if not latest:
            return action_error("No background tasks have been submitted yet.")
        task_id = latest["id"]

    success = task_queue.cancel_task(task_id)
    if success:
        return action_ok(f"Task {task_id} has been cancelled.", task_id=task_id)

    task = task_queue.get_task(task_id)
    if not task:
        return action_error(f"Task '{task_id}' was not found.")

    return action_ok(f"Task {task_id} is already in state '{task['status']}' and cannot be cancelled.", task_id=task_id)


TOOL = {
    "name": "cancel_task",
    "description": (
        "Cancel a pending or running background task. "
        "Call when the user says: cancel task, abort task, stop the background job, cancel latest task."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "task_id": {
                "type": "STRING",
                "description": "Task ID to cancel, or 'latest' (default) to cancel the most recent task.",
            },
        },
        "required": [],
    },
    "handler": _cancel_task,
}
