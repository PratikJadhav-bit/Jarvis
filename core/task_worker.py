"""
core/task_worker.py — Background worker engine for Mark-LIII.

Executes tasks outside the Gemini Live voice session on a dedicated daemon
worker thread. Tasks survive voice reconnects, pauses, and session restarts.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from core import task_queue
from core import notifier
from core import policy
from core import kill_switch
from core.result import ActionResult, to_action_result

_log = logging.getLogger("task_worker")


def _default_execute_task(task: dict[str, Any], is_cancelled_fn: Callable[[], bool]) -> tuple[bool, str, list[dict]]:
    """
    Default execution engine for a task:
    Uses the real agent loop (observe -> decide -> act -> verify).
    """
    goal = task.get("goal", "")
    task_id = task.get("id", "")

    from core.agent_loop import AgentLoop
    loop = AgentLoop(goal=goal, task_id=task_id)
    return loop.run(is_cancelled_fn=is_cancelled_fn)


class TaskWorker:
    """Manages the background execution thread and task processing loop."""

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._wake_event = threading.Event()
        self._current_task_id: Optional[str] = None
        self._custom_executor: Optional[Callable[[dict, Callable[[], bool]], tuple[bool, str, list[dict]]]] = None
        self._say_fn: Optional[Callable[[str], None]] = None

        # Hook into global kill switch
        kill_switch.add_stop_callback(self._on_kill_switch)

    def set_speech_callback(self, say_fn: Callable[[str], None]) -> None:
        """Set callback to speak notifications aloud when user is active."""
        self._say_fn = say_fn

    def set_custom_executor(self, fn: Optional[Callable]) -> None:
        """Override task executor (useful for testing or specialized task handlers)."""
        self._custom_executor = fn

    def start(self) -> None:
        """Start the background worker thread (idempotent)."""
        if self._running and self._thread and self._thread.is_alive():
            return

        # Recover any tasks stuck from previous crash/restart
        task_queue.recover_stale_tasks()

        self._running = True
        self._wake_event.clear()
        self._thread = threading.Thread(target=self._worker_loop, name="JarvisTaskWorker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the worker loop."""
        self._running = False
        self._wake_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def submit(self, goal: str, task_id: Optional[str] = None) -> str:
        """Submit a new task to SQLite queue and wake up worker."""
        tid = task_queue.create_task(goal, task_id=task_id)
        self.wake()
        return tid

    def wake(self) -> None:
        """Signal worker thread that a task is available."""
        self._wake_event.set()

    def current_task_id(self) -> Optional[str]:
        return self._current_task_id

    def _on_kill_switch(self) -> None:
        """Triggered immediately when global kill switch fires."""
        task_queue.cancel_all_active_tasks()
        self.wake()

    def _is_task_cancelled(self, task_id: str) -> bool:
        if kill_switch.is_paused():
            return True
        try:
            t = task_queue.get_task(task_id)
            return t is None or t.get("status") == "CANCELLED"
        except Exception:
            return True

    def _worker_loop(self) -> None:
        while self._running:
            # Check for PENDING tasks
            try:
                pending = task_queue.list_tasks(status="PENDING", limit=1)
            except Exception:
                if not self._running:
                    break
                self._wake_event.wait(timeout=1.0)
                continue

            if not pending:
                # Wait for next submission or heartbeat
                self._wake_event.wait(timeout=2.0)
                self._wake_event.clear()
                continue

            task = pending[0]
            task_id = task["id"]
            goal = task["goal"]

            if self._is_task_cancelled(task_id):
                continue

            # Mark task as RUNNING
            self._current_task_id = task_id
            task_queue.update_task(task_id, status="RUNNING")

            executor = self._custom_executor or _default_execute_task

            try:
                ok, message, steps = executor(task, lambda: self._is_task_cancelled(task_id))
            except Exception as e:
                ok = False
                message = f"Worker exception: {e}"
                steps = []

            try:
                # Check if cancelled during execution
                if self._is_task_cancelled(task_id):
                    task_queue.update_task(task_id, status="CANCELLED", error="Cancelled during run", steps=steps)
                    self._current_task_id = None
                    continue

                # Update final status in DB
                new_status = "COMPLETED" if ok else "FAILED"
                task_queue.update_task(
                    task_id,
                    status=new_status,
                    steps=steps,
                    result=message if ok else None,
                    error=None if ok else message,
                    unspoken=1
                )

                self._current_task_id = None

                # Notify user via Windows Toast & active speech
                notifier.notify_task_completion(
                    task_id=task_id,
                    goal=goal,
                    status=new_status,
                    result=message if ok else None,
                    error=None if ok else message,
                    say_fn=self._say_fn
                )
            except Exception as e:
                _log.warning(f"Error finalizing task {task_id}: {e}")
                self._current_task_id = None


# ── Global singleton worker ──────────────────────────────────────────────────

_worker_instance: Optional[TaskWorker] = None
_worker_lock = threading.Lock()


def get_worker() -> TaskWorker:
    global _worker_instance
    with _worker_lock:
        if _worker_instance is None:
            _worker_instance = TaskWorker()
        return _worker_instance


def start_worker() -> TaskWorker:
    w = get_worker()
    w.start()
    return w


def stop_worker(timeout: float = 2.0) -> None:
    """Stop the global worker singleton (for shutdown or tests)."""
    global _worker_instance
    with _worker_lock:
        if _worker_instance is not None:
            _worker_instance.stop(timeout=timeout)
            _worker_instance = None
