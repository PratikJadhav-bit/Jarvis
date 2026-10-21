"""
actions/submit_task.py — Voice-facing tool to submit background tasks.

Returns immediately with a task ID so the Gemini Live voice session never blocks.
"""
from __future__ import annotations

from core import task_worker
from core import learning
from core.result import ActionResult, action_ok, action_error


def _submit_task(parameters: dict, **_) -> ActionResult:
    goal = str(parameters.get("goal") or "").strip()
    if not goal:
        return action_error("Please provide a goal for the background task.")

    worker = task_worker.get_worker()
    worker.start()
    task_id = worker.submit(goal)

    try:
        learning.log_command(raw_text=goal, interpreted_goal=goal, task_id=task_id)
    except Exception:
        pass

    msg = f"Task submitted with ID {task_id}. I am working on '{goal[:60]}' in the background and will notify you when it finishes."
    return action_ok(msg, task_id=task_id, goal=goal)


TOOL = {
    "name": "submit_task",
    "description": (
        "Submit a long-running, multi-step, or background task to the JARVIS agent worker. "
        "Returns immediately with a task ID so you can continue talking to the user. "
        "Use this for tasks that take more than an instant (e.g. searching, processing files, "
        "organizing folders, downloading, multi-step browser tasks). "
        "The worker will execute the goal in the background and notify the user via Windows toast."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "goal": {
                "type": "STRING",
                "description": "The user's objective or goal to accomplish in the background.",
            },
        },
        "required": ["goal"],
    },
    "handler": _submit_task,
}
