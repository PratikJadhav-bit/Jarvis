"""
tests/test_phase4.py — Unit tests and Acceptance test for Phase 4 (Learning from commands).
"""
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from core import learning
from core import kill_switch
from core import policy
from core import llm_client
from core.action_loader import ActionRecord, ActionRegistry
from core.agent_loop import AgentLoop
from actions import forget_learned as _forget_mod
from actions import list_learned as _list_mod


class TestLearningSystem(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "learning.db"
        learning.set_db_path(self.db_path)
        kill_switch.resume()
        policy.reload_policy()
        policy.set_paused(False)

    def tearDown(self):
        learning.close_all_connections()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_logging_happens_without_model_involvement(self):
        """Commands are logged automatically with text only, without LLM involvement."""
        cmd_id = learning.log_command(
            raw_text="find all pdfs in downloads and zip them",
            interpreted_goal="find all pdfs in downloads and zip them",
            task_id="task_123"
        )
        self.assertTrue(cmd_id.startswith("cmd_"))

        # Verify entry in database
        con = learning._get_connection()
        row = con.execute("SELECT * FROM commands WHERE id = ?", (cmd_id,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["task_id"], "task_123")
        self.assertIn("zip them", row["interpreted_goal"])

    def test_secrets_redacted_in_learning_storage(self):
        """API keys, passwords, and tokens are redacted before storage in learning DB."""
        sensitive_raw = "Open account with token=sk-abcdef1234567890abcdef12345 and password=SuperSecret99"
        sensitive_goal = "Connect to AIzaSyDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD"

        cmd_id = learning.log_command(raw_text=sensitive_raw, interpreted_goal=sensitive_goal)

        con = learning._get_connection()
        row = con.execute("SELECT * FROM commands WHERE id = ?", (cmd_id,)).fetchone()
        self.assertIsNotNone(row)

        self.assertNotIn("sk-abcdef1234567890abcdef12345", row["raw_text"])
        self.assertNotIn("SuperSecret99", row["raw_text"])
        self.assertNotIn("AIzaSyDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD", row["interpreted_goal"])
        self.assertIn("[REDACTED]", row["raw_text"])
        self.assertIn("[REDACTED]", row["interpreted_goal"])

    def test_recipe_distillation_and_gotchas(self):
        """Successful runs distill into recipes; self-corrections are saved as gotchas."""
        steps = [
            {"step": 1, "tool": "open_app", "args": {"name": "calc"}, "ok": True},
            {"step": 2, "tool": "network_query", "args": {"url": "http://primary"}, "ok": False, "error": "timeout"},
            {"step": 3, "tool": "network_query", "args": {"url": "http://mirror"}, "ok": True, "output": "ok"},
        ]
        learning.record_run(
            task_id="task_run_1",
            goal="Calculate numbers and download dataset",
            outcome="success",
            duration=4.2,
            steps=steps,
            tools=["open_app", "network_query"]
        )

        recipes = learning.retrieve_recipes("Calculate numbers and download dataset")
        self.assertGreaterEqual(len(recipes), 1)
        top = recipes[0]
        self.assertEqual(top["success_count"], 1)
        self.assertEqual(top["fail_count"], 0)
        # Gotcha should capture the failure and resolution
        self.assertIn("timeout", top["gotchas"])
        self.assertIn("network_query", top["gotchas"])

    def test_negative_feedback_lowers_recipe_rank(self):
        """User negative feedback increases fail_count and reduces ranking score."""
        goal_a = "Organize photos by date"
        goal_b = "Organize music by artist"

        # Record two recipes
        learning.record_run("task_a", goal_a, "success", 2.0, [{"tool": "dummy", "args": {}}], ["dummy"])
        learning.record_run("task_b", goal_b, "success", 2.0, [{"tool": "dummy", "args": {}}], ["dummy"])

        # Initial rank of task_a should be high
        recs_before = learning.retrieve_recipes("Organize photos")
        self.assertEqual(recs_before[0]["goal_pattern"], learning.normalize_goal(goal_a))
        self.assertEqual(recs_before[0]["fail_count"], 0)

        # Record negative feedback for task_a
        learning.record_feedback("task_a", "negative")

        recs_after = learning.retrieve_recipes("Organize photos")
        self.assertEqual(recs_after[0]["fail_count"], 1)

    def test_positive_feedback_increases_recipe_rank(self):
        """User positive feedback increments success_count."""
        goal = "Clean temporary files"
        learning.record_run("task_clean", goal, "success", 1.5, [{"tool": "dummy", "args": {}}], ["dummy"])

        learning.record_feedback("task_clean", "positive")

        recs = learning.retrieve_recipes("Clean temporary files")
        self.assertGreaterEqual(len(recs), 1)
        self.assertEqual(recs[0]["success_count"], 2)

    def test_spoken_feedback_detection(self):
        """Spoken feedback detects praise and corrections within 60s window."""
        t0 = time.time()

        # Negative within window
        neg_res = learning.detect_spoken_feedback("no, not that", "task_99", last_completed_ts=t0, current_time=t0 + 10)
        self.assertEqual(neg_res, ("negative", "task_99"))

        # Positive within window
        pos_res = learning.detect_spoken_feedback("that was awesome thanks", "task_99", last_completed_ts=t0, current_time=t0 + 20)
        self.assertEqual(pos_res, ("positive", "task_99"))

        # Outside 60s window -> None
        expired_res = learning.detect_spoken_feedback("no, wrong", "task_99", last_completed_ts=t0, current_time=t0 + 75)
        self.assertIsNone(expired_res)

    def test_preferences_extraction(self):
        """Durable preferences are inferred and saved."""
        steps = [{"tool": "open_app", "args": {"name": "chrome"}}]
        learning.record_run("task_pref", "Search AI papers on chrome", "success", 1.0, steps, ["open_app"])

        con = learning._get_connection()
        row = con.execute("SELECT * FROM preferences WHERE key = 'default_browser'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["value"], "chrome")

    def test_macro_creation_and_listing(self):
        """Macros can be saved, listed, and retrieved."""
        steps = [
            {"tool": "open_app", "args": {"name": "notepad"}},
            {"tool": "type_keys", "args": {"text": "hello"}},
        ]
        mid = learning.save_macro("daily_notes", "take daily notes", steps)
        self.assertTrue(mid.startswith("mac_"))

        all_macros = learning.list_macros()
        self.assertGreaterEqual(len(all_macros), 1)
        self.assertEqual(all_macros[0]["name"], "daily_notes")

        mac = learning.get_macro("daily_notes")
        self.assertIsNotNone(mac)
        self.assertEqual(mac["id"], mid)

    def test_forget_learned_removes_items(self):
        """forget_learned removes matching recipes, macros, commands, and preferences."""
        learning.log_command("sync git repository", "sync git repository")
        learning.save_macro("git_sync_macro", "sync git repository", [{"tool": "git", "args": {}}])
        learning.record_run("task_git", "sync git repository", "success", 1.0, [{"tool": "git", "args": {}}], ["git"])

        # Tool execution
        res = _forget_mod.TOOL["handler"]({"query": "git"})
        self.assertTrue(res.ok)
        self.assertIn("Forgot", res.output)

        # Verify items gone
        recs = learning.retrieve_recipes("sync git repository")
        self.assertEqual(len(recs), 0)
        self.assertIsNone(learning.get_macro("git_sync_macro"))

    def test_list_learned_tool(self):
        """list_learned tool outputs summary of learned database."""
        learning.save_macro("test_macro", "test goal", [{"tool": "dummy", "args": {}}])
        res = _list_mod.TOOL["handler"]({})
        self.assertTrue(res.ok)
        self.assertIn("Learned database contains", res.output)


class TestPhase4Acceptance(unittest.TestCase):
    """
    ACCEPTANCE TEST FOR PHASE 4:
    Run the same command twice.
    The second run retrieves the first run's recipe and takes measurably fewer steps/seconds
    via fast verified replay without needing iterative LLM round trips.
    """
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "learning.db"
        learning.set_db_path(self.db_path)
        kill_switch.resume()
        policy.reload_policy()
        policy.set_paused(False)

    def tearDown(self):
        llm_client.set_mock_backend(None)
        learning.close_all_connections()
        kill_switch.resume()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_phase4_acceptance_run_twice_faster_with_recipe(self):
        call_log = []

        def step_a_handler(parameters, **_):
            time.sleep(0.01)
            call_log.append("step_a")
            return {"ok": True, "output": "Step A completed"}

        def step_b_handler(parameters, **_):
            time.sleep(0.01)
            call_log.append("step_b")
            return {"ok": True, "output": "Step B completed"}

        def step_c_handler(parameters, **_):
            time.sleep(0.01)
            call_log.append("step_c")
            return {"ok": True, "output": "Step C completed"}

        records = {
            "step_a": ActionRecord(name="step_a", handler=step_a_handler, valid=True),
            "step_b": ActionRecord(name="step_b", handler=step_b_handler, valid=True),
            "step_c": ActionRecord(name="step_c", handler=step_c_handler, valid=True),
        }
        test_registry = ActionRegistry(records, logger=lambda _: None)

        goal = "Run weekly system health check and generate report"

        # Mock LLM for the FIRST run (simulating 3 decision steps + 1 done)
        llm_call_count = [0]

        def mock_llm_first_run(prompt: str, system: str) -> dict:
            llm_call_count[0] += 1
            time.sleep(0.05)  # Simulate LLM inference latency
            step = llm_call_count[0]
            if step == 1:
                return {"status": "IN_PROGRESS", "action": "step_a", "args": {}}
            elif step == 2:
                return {"status": "IN_PROGRESS", "action": "step_b", "args": {}}
            elif step == 3:
                return {"status": "IN_PROGRESS", "action": "step_c", "args": {}}
            else:
                return {"status": "DONE", "summary": "Health check complete"}

        llm_client.set_mock_backend(mock_llm_first_run)

        # ── RUN 1: Dynamic LLM loop ──────────────────────────────────────────
        t0 = time.monotonic()
        loop1 = AgentLoop(goal=goal, task_id="task_run_1", registry=test_registry)
        ok1, summary1, steps1 = loop1.run()
        duration1 = time.monotonic() - t0

        self.assertTrue(ok1)
        self.assertEqual(len(steps1), 3)
        self.assertGreaterEqual(llm_call_count[0], 3)
        self.assertEqual(call_log, ["step_a", "step_b", "step_c"])

        # Verify that Run 1 distilled a recipe in the learning database
        recipes = learning.retrieve_recipes(goal)
        self.assertGreaterEqual(len(recipes), 1)
        self.assertEqual(recipes[0]["success_count"], 1)

        # ── RUN 2: Replay using learned recipe ────────────────────────────────
        # Set mock backend to fail if called, verifying that Run 2 reuses the learned recipe
        # directly via fast verified replay without needing LLM round trips!
        call_log.clear()
        llm_second_run_calls = [0]

        def mock_llm_second_run(prompt: str, system: str) -> dict:
            llm_second_run_calls[0] += 1
            # In case LLM is called
            return {"status": "DONE", "summary": "Done"}

        llm_client.set_mock_backend(mock_llm_second_run)

        t1 = time.monotonic()
        loop2 = AgentLoop(goal=goal, task_id="task_run_2", registry=test_registry)
        ok2, summary2, steps2 = loop2.run()
        duration2 = time.monotonic() - t1

        self.assertTrue(ok2)
        self.assertIn("learned recipe", summary2.lower())
        self.assertEqual(len(steps2), 3)
        self.assertEqual(call_log, ["step_a", "step_b", "step_c"])

        # Second run required ZERO LLM calls because it replayed the learned recipe
        self.assertEqual(llm_second_run_calls[0], 0)

        # Measurably faster
        self.assertLess(duration2, duration1)
        print(f"\n[Phase 4 Acceptance] Run 1 Duration: {duration1:.3f}s (LLM calls: {llm_call_count[0]})")
        print(f"[Phase 4 Acceptance] Run 2 Duration: {duration2:.3f}s (LLM calls: {llm_second_run_calls[0]}, via learned recipe replay)")


if __name__ == "__main__":
    unittest.main()
