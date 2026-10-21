"""
tests/test_phase2.py — Acceptance tests for Phase 2: Background Task Runner.

Tests run entirely offline with an isolated test SQLite database.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core import task_queue
from core import task_worker
from core import kill_switch
from core import policy
from actions.submit_task import _submit_task
from actions.task_status import _task_status
from actions.cancel_task import _cancel_task
from actions.list_tasks import _list_tasks


class TestTaskQueue(unittest.TestCase):
    def setUp(self):
        task_worker.stop_worker(timeout=0.5)
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = Path(self._temp_dir.name) / "test_tasks.db"
        task_queue.set_custom_db_path(self._db_path)

    def tearDown(self):
        task_worker.stop_worker(timeout=0.5)
        task_queue.close_all_connections()
        try:
            self._temp_dir.cleanup()
        except Exception:
            pass

    def test_create_and_get_task(self):
        tid = task_queue.create_task("Organize Downloads folder")
        self.assertTrue(tid.startswith("task_"))

        task = task_queue.get_task(tid)
        self.assertIsNotNone(task)
        self.assertEqual(task["goal"], "Organize Downloads folder")
        self.assertEqual(task["status"], "PENDING")
        self.assertEqual(task["steps"], [])
        self.assertEqual(task["unspoken"], 1)

    def test_get_latest_task(self):
        tid1 = task_queue.create_task("Task 1")
        time.sleep(0.01)
        tid2 = task_queue.create_task("Task 2")

        latest = task_queue.get_latest_task()
        self.assertIsNotNone(latest)
        self.assertEqual(latest["id"], tid2)
        self.assertEqual(latest["goal"], "Task 2")

    def test_update_task_fields(self):
        tid = task_queue.create_task("Initial goal")
        steps = [{"step": 1, "tool": "file_controller", "success": True}]
        ok = task_queue.update_task(
            tid,
            status="COMPLETED",
            steps=steps,
            result="All files organized",
            error=None
        )
        self.assertTrue(ok)

        task = task_queue.get_task(tid)
        self.assertEqual(task["status"], "COMPLETED")
        self.assertEqual(len(task["steps"]), 1)
        self.assertEqual(task["result"], "All files organized")

    def test_cancel_task(self):
        tid = task_queue.create_task("Task to cancel")
        cancelled = task_queue.cancel_task(tid)
        self.assertTrue(cancelled)

        task = task_queue.get_task(tid)
        self.assertEqual(task["status"], "CANCELLED")

        # Cancelling an already cancelled task returns False
        cancelled_again = task_queue.cancel_task(tid)
        self.assertFalse(cancelled_again)

    def test_cancel_all_active_tasks(self):
        t1 = task_queue.create_task("Task 1")
        t2 = task_queue.create_task("Task 2")
        task_queue.update_task(t1, status="RUNNING")

        count = task_queue.cancel_all_active_tasks()
        self.assertEqual(count, 2)

        self.assertEqual(task_queue.get_task(t1)["status"], "CANCELLED")
        self.assertEqual(task_queue.get_task(t2)["status"], "CANCELLED")

    def test_recover_stale_tasks(self):
        """Tasks left in RUNNING on app restart are gracefully recovered as FAILED."""
        tid = task_queue.create_task("Task interrupted by power loss")
        task_queue.update_task(tid, status="RUNNING")

        recovered = task_queue.recover_stale_tasks()
        self.assertIn(tid, recovered)

        task = task_queue.get_task(tid)
        self.assertEqual(task["status"], "FAILED")
        self.assertIn("restart", task["error"].lower())

    def test_unspoken_notifications(self):
        tid = task_queue.create_task("Task for notification")
        task_queue.update_task(tid, status="COMPLETED", result="Success")

        unspoken = task_queue.get_unspoken_notifications()
        self.assertEqual(len(unspoken), 1)
        self.assertEqual(unspoken[0]["id"], tid)

        task_queue.mark_spoken([tid])
        unspoken_after = task_queue.get_unspoken_notifications()
        self.assertEqual(len(unspoken_after), 0)


class TestVoiceTools(unittest.TestCase):
    def setUp(self):
        task_worker.stop_worker(timeout=0.5)
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = Path(self._temp_dir.name) / "test_tasks.db"
        task_queue.set_custom_db_path(self._db_path)

    def tearDown(self):
        task_worker.stop_worker(timeout=0.5)
        task_queue.close_all_connections()
        try:
            self._temp_dir.cleanup()
        except Exception:
            pass

    def test_submit_task_returns_instantly(self):
        """submit_task must return immediately with a task ID (< 0.5s)."""
        goal = "find all PDFs in Downloads and zip them"
        t0 = time.monotonic()
        res = _submit_task({"goal": goal})
        elapsed = time.monotonic() - t0

        self.assertTrue(res.ok)
        self.assertLess(elapsed, 0.5, "submit_task must return instantly without blocking")
        self.assertTrue(res.output.startswith("Task submitted with ID task_"))
        tid = res.data.get("task_id")
        self.assertIsNotNone(tid)

        task = task_queue.get_task(tid)
        self.assertIsNotNone(task)
        self.assertEqual(task["goal"], goal)

    def test_submit_task_empty_goal_error(self):
        res = _submit_task({"goal": "   "})
        self.assertFalse(res.ok)
        self.assertIn("goal", res.error.lower())

    def test_task_status_latest(self):
        tid = task_queue.create_task("Compile project")
        task_queue.update_task(tid, status="RUNNING", steps=[{"step": 1, "tool": "code_helper", "success": True}])

        res = _task_status({"task_id": "latest"})
        self.assertTrue(res.ok)
        self.assertIn(tid, res.output)
        self.assertIn("RUNNING", res.output)
        self.assertIn("code_helper", res.output)

    def test_task_status_by_id(self):
        tid = task_queue.create_task("Specific task")
        task_queue.update_task(tid, status="COMPLETED", result="Done successfully")

        res = _task_status({"task_id": tid})
        self.assertTrue(res.ok)
        self.assertIn("COMPLETED", res.output)
        self.assertIn("Done successfully", res.output)

    def test_cancel_task_tool(self):
        tid = task_queue.create_task("Task to cancel via tool")
        res = _cancel_task({"task_id": tid})
        self.assertTrue(res.ok)
        self.assertIn("cancelled", res.output.lower())

        status = task_queue.get_task(tid)["status"]
        self.assertEqual(status, "CANCELLED")

    def test_list_tasks_tool(self):
        t1 = task_queue.create_task("Task A")
        t2 = task_queue.create_task("Task B")

        res = _list_tasks({"limit": 5})
        self.assertTrue(res.ok)
        self.assertIn("Task A", res.output)
        self.assertIn("Task B", res.output)
        self.assertEqual(res.data.get("count"), 2)


class TestTaskWorker(unittest.TestCase):
    def setUp(self):
        task_worker.stop_worker(timeout=0.5)
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = Path(self._temp_dir.name) / "test_tasks.db"
        task_queue.set_custom_db_path(self._db_path)
        kill_switch.resume()
        self.worker = task_worker.get_worker()

    def tearDown(self):
        task_worker.stop_worker(timeout=1.0)
        kill_switch.resume()
        task_queue.close_all_connections()
        try:
            self._temp_dir.cleanup()
        except Exception:
            pass

    def test_worker_processes_task_to_completion(self):
        """Worker executes submitted task outside the main thread and updates status."""
        executed = threading.Event()

        def mock_executor(task, is_cancelled):
            time.sleep(0.05)
            executed.set()
            return True, "Zipped 5 PDF files into downloads.zip", [{"step": 1, "tool": "file_controller", "success": True}]

        self.worker.set_custom_executor(mock_executor)
        self.worker.start()

        # Submit task
        tid = self.worker.submit("find all PDFs in Downloads and zip them")

        # Wait for worker to finish
        self.assertTrue(executed.wait(timeout=3.0), "Worker did not execute task in time")

        # Poll until DB status is COMPLETED
        for _ in range(30):
            task = task_queue.get_task(tid)
            if task and task["status"] == "COMPLETED":
                break
            time.sleep(0.05)

        task = task_queue.get_task(tid)
        self.assertEqual(task["status"], "COMPLETED")
        self.assertIn("Zipped 5 PDF files", task["result"])
        self.assertEqual(len(task["steps"]), 1)
        self.assertEqual(task["unspoken"], 1)

    def test_worker_cancellation_during_execution(self):
        """Cancelled task is halted immediately."""
        started = threading.Event()

        def slow_executor(task, is_cancelled):
            started.set()
            for _ in range(50):
                if is_cancelled():
                    return False, "Cancelled", []
                time.sleep(0.02)
            return True, "Finished", []

        self.worker.set_custom_executor(slow_executor)
        self.worker.start()

        tid = self.worker.submit("Long running task")
        self.assertTrue(started.wait(timeout=2.0))

        # Cancel while it is running
        ok = task_queue.cancel_task(tid)
        self.assertTrue(ok)
        self.worker.wake()

        # Wait for task status to become CANCELLED
        for _ in range(30):
            task = task_queue.get_task(tid)
            if task and task["status"] == "CANCELLED":
                break
            time.sleep(0.05)

        task = task_queue.get_task(tid)
        self.assertEqual(task["status"], "CANCELLED")

    def test_kill_switch_cancels_worker_tasks(self):
        """Triggering kill switch cancels all running worker tasks and pauses policy."""
        started = threading.Event()

        def slow_executor(task, is_cancelled):
            started.set()
            for _ in range(50):
                if is_cancelled():
                    return False, "Cancelled by kill switch", []
                time.sleep(0.02)
            return True, "Finished", []

        self.worker.set_custom_executor(slow_executor)
        self.worker.start()

        tid = self.worker.submit("Task to be killed")
        self.assertTrue(started.wait(timeout=2.0))

        # Fire global emergency stop
        kill_switch.trigger()

        # Verify policy is paused
        self.assertTrue(policy.is_paused())

        # Verify task is cancelled
        for _ in range(30):
            task = task_queue.get_task(tid)
            if task and task["status"] == "CANCELLED":
                break
            time.sleep(0.05)

        task = task_queue.get_task(tid)
        self.assertEqual(task["status"], "CANCELLED")
        kill_switch.resume()

    def test_voice_remains_usable_while_worker_runs(self):
        """Voice/read tools can run concurrently while background task executes."""
        worker_busy = threading.Event()
        release_worker = threading.Event()

        def blocking_executor(task, is_cancelled):
            worker_busy.set()
            release_worker.wait(timeout=2.0)
            return True, "Done", []

        self.worker.set_custom_executor(blocking_executor)
        self.worker.start()

        tid = self.worker.submit("Heavy background job")
        self.assertTrue(worker_busy.wait(timeout=2.0))

        # While worker is blocked in background, call read tools
        from core import policy
        decision = policy.check("system_status", {})
        self.assertEqual(decision.tier, policy.Tier.AUTO)

        status_res = _task_status({"task_id": tid})
        self.assertTrue(status_res.ok)
        self.assertIn("RUNNING", status_res.output)

        release_worker.set()


if __name__ == "__main__":
    unittest.main()
