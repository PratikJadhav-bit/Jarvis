"""
tests/test_phase3.py — Acceptance tests for Phase 3: Real Agent Loop.

Acceptance test criteria:
  - Ph3: a 5+ step task (open app, do something, verify) completes and
    self-corrects once after an injected failure.
  - run_powershell tool: output-capped, timeout enforcement, policy-gated.
  - ui_automation tool: action dispatching.
  - AgentLoop: observe -> decide -> act -> verify loop with original goal preserved.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core import llm_client
from core import policy
from core import kill_switch
from core import learning
from core.agent_loop import AgentLoop
from core.action_loader import ActionRecord, ActionRegistry
from actions.run_powershell import _run_powershell, _MAX_OUTPUT_CHARS
from actions.ui_automation import _ui_automation


class TestRunPowershellTool(unittest.TestCase):
    def setUp(self):
        policy.reload_policy()
        policy.set_paused(False)

    def test_run_powershell_simple_command(self):
        res = _run_powershell({"command": "Write-Output 'Hello Jarvis'"})
        self.assertTrue(res.ok)
        self.assertIn("Hello Jarvis", res.output)
        self.assertEqual(res.data.get("returncode"), 0)

    def test_run_powershell_output_capping(self):
        """Output exceeding _MAX_OUTPUT_CHARS is truncated cleanly."""
        res = _run_powershell({"command": f"Write-Output ('A' * {_MAX_OUTPUT_CHARS + 1000})"})
        self.assertTrue(res.ok)
        self.assertIn("truncated", res.output)
        self.assertLessEqual(len(res.data.get("stdout")), _MAX_OUTPUT_CHARS + 100)

    def test_run_powershell_timeout(self):
        """Commands exceeding timeout are killed."""
        t0 = time.monotonic()
        res = _run_powershell({"command": "Start-Sleep -Seconds 5", "timeout": 0.5})
        elapsed = time.monotonic() - t0
        self.assertFalse(res.ok)
        self.assertIn("timed out", res.error.lower())
        self.assertLess(elapsed, 2.5)

    def test_run_powershell_policy_gated_confirm(self):
        """run_powershell is classified as CONFIRM in policy."""
        d = policy.check("run_powershell", {"command": "Get-Process"})
        self.assertEqual(d.tier, policy.Tier.CONFIRM)

    def test_run_powershell_policy_gated_deny(self):
        """Dangerous commands are hard DENIED by policy."""
        d = policy.check("run_powershell", {"command": "Format-Volume -DriveLetter C"})
        self.assertEqual(d.tier, policy.Tier.DENY)


class TestUIAutomationTool(unittest.TestCase):
    def test_ui_automation_list_windows(self):
        res = _ui_automation({"action": "list_windows"})
        self.assertTrue(res.ok)
        self.assertIn("Active windows", res.output)
        self.assertIsInstance(res.data.get("windows"), list)

    def test_ui_automation_missing_title_error(self):
        res = _ui_automation({"action": "focus", "title": ""})
        self.assertFalse(res.ok)
        self.assertIn("missing", res.error.lower())


class TestAgentLoop(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        learning.set_db_path(Path(self._temp_dir.name) / "learning.db")
        kill_switch.resume()
        policy.reload_policy()
        policy.set_paused(False)

    def tearDown(self):
        llm_client.set_mock_backend(None)
        learning.close_all_connections()
        kill_switch.resume()
        try:
            self._temp_dir.cleanup()
        except Exception:
            pass

    def test_agent_loop_5_steps_with_self_correction(self):
        """
        Acceptance Test for Phase 3:
        A 5+ step task (open app, do something, verify) completes and
        self-corrects once after an injected failure.
        """
        # Define mock actions for the registry
        action_log = []
        injected_failure_occurred = [False]

        def open_app_handler(parameters, **_):
            action_log.append(("open_app", parameters))
            return {"ok": True, "output": f"App '{parameters.get('name')}' launched"}

        def navigate_handler(parameters, **_):
            action_log.append(("navigate", parameters))
            # Inject failure on the first attempt!
            if not injected_failure_occurred[0]:
                injected_failure_occurred[0] = True
                return {"ok": False, "error": "Connection reset by peer at path /data"}
            return {"ok": True, "output": f"Navigated successfully to {parameters.get('path')}"}

        def process_data_handler(parameters, **_):
            action_log.append(("process_data", parameters))
            return {"ok": True, "output": f"Processed {parameters.get('count')} records"}

        def export_results_handler(parameters, **_):
            action_log.append(("export_results", parameters))
            return {"ok": True, "output": f"Results saved to {parameters.get('dest')}"}

        def verify_export_handler(parameters, **_):
            action_log.append(("verify_export", parameters))
            return {"ok": True, "output": "Verified export checksum matches 100%"}

        records = {
            "open_app": ActionRecord(name="open_app", handler=open_app_handler, valid=True),
            "navigate": ActionRecord(name="navigate", handler=navigate_handler, valid=True),
            "process_data": ActionRecord(name="process_data", handler=process_data_handler, valid=True),
            "export_results": ActionRecord(name="export_results", handler=export_results_handler, valid=True),
            "verify_export": ActionRecord(name="verify_export", handler=verify_export_handler, valid=True),
        }
        test_registry = ActionRegistry(records, logger=lambda _: None)

        # Mock LLM decision function that observes the injected failure and self-corrects
        goal = "Open data manager, navigate to records, process data, export report, and verify"
        step_counter = [0]

        def mock_llm_decide(prompt: str, system: str) -> dict:
            step_counter[0] += 1
            # Verify original goal is always in the prompt
            self.assertIn("ORIGINAL GOAL: " + goal, prompt)

            step = step_counter[0]
            if step == 1:
                return {
                    "status": "IN_PROGRESS",
                    "action": "open_app",
                    "args": {"name": "data_manager"},
                    "description": "Open data manager",
                    "verification_strategy": "Check app launched"
                }
            elif step == 2:
                return {
                    "status": "IN_PROGRESS",
                    "action": "navigate",
                    "args": {"path": "/data/records"},
                    "description": "Navigate to records",
                    "verification_strategy": "Check navigation successful"
                }
            elif step == 3:
                # Self-correction after observing failure
                self.assertIn("Connection reset by peer", prompt)
                return {
                    "status": "IN_PROGRESS",
                    "action": "navigate",
                    "args": {"path": "/data/records_retry_mirror"},
                    "description": "Self-correction: retry navigation with backup mirror",
                    "verification_strategy": "Check navigation successful"
                }
            elif step == 4:
                return {
                    "status": "IN_PROGRESS",
                    "action": "process_data",
                    "args": {"count": 100},
                    "description": "Process data records",
                    "verification_strategy": "Check records processed"
                }
            elif step == 5:
                return {
                    "status": "IN_PROGRESS",
                    "action": "export_results",
                    "args": {"dest": "report.pdf"},
                    "description": "Export results to PDF",
                    "verification_strategy": "Check export file exists"
                }
            elif step == 6:
                return {
                    "status": "IN_PROGRESS",
                    "action": "verify_export",
                    "args": {"dest": "report.pdf"},
                    "description": "Verify export checksum",
                    "verification_strategy": "Check checksum"
                }
            else:
                return {
                    "status": "DONE",
                    "summary": "Data manager opened, records navigated via fallback mirror, processed, exported, and verified"
                }

        llm_client.set_mock_backend(mock_llm_decide)

        loop = AgentLoop(
            goal=goal,
            task_id="task_test_ph3",
            max_steps=10,
            registry=test_registry,
        )

        ok, summary, steps = loop.run()

        self.assertTrue(ok)
        self.assertIn("verified", summary.lower())

        # Verify task had 5+ steps executed (1: open_app, 2: navigate [failed], 3: navigate [retry], 4: process_data, 5: export, 6: verify)
        self.assertGreaterEqual(len(steps), 5)

        # Verify self-correction occurred: step 2 failed, step 3 succeeded with alternative path
        failed_step = steps[1]
        self.assertFalse(failed_step["ok"])
        self.assertEqual(failed_step["tool"], "navigate")

        retry_step = steps[2]
        self.assertTrue(retry_step["ok"])
        self.assertEqual(retry_step["tool"], "navigate")
        self.assertIn("records_retry_mirror", retry_step["args"]["path"])

        # Final verification step succeeded
        last_step = steps[-1]
        self.assertEqual(last_step["tool"], "verify_export")
        self.assertTrue(last_step["verified"])

    def test_agent_loop_time_budget_enforcement(self):
        """Loop terminates when time budget expires."""
        def dummy_handler(parameters, **_):
            time.sleep(0.05)
            return {"ok": True, "output": "step done"}

        records = {"dummy": ActionRecord(name="dummy", handler=dummy_handler, valid=True)}
        test_registry = ActionRegistry(records, logger=lambda _: None)

        def infinite_decide(prompt, system):
            return {
                "status": "IN_PROGRESS",
                "action": "dummy",
                "args": {},
            }

        llm_client.set_mock_backend(infinite_decide)

        loop = AgentLoop(
            goal="Infinite task",
            time_budget=0.1,  # 100ms budget
            max_steps=100,
            registry=test_registry,
        )
        ok, error, steps = loop.run()
        self.assertFalse(ok)
        self.assertIn("budget", error.lower())

    def test_agent_loop_max_steps_enforcement(self):
        """Loop terminates when max steps is reached."""
        def dummy_handler(parameters, **_):
            return {"ok": True, "output": "step done"}

        records = {"dummy": ActionRecord(name="dummy", handler=dummy_handler, valid=True)}
        test_registry = ActionRegistry(records, logger=lambda _: None)

        def infinite_decide(prompt, system):
            return {
                "status": "IN_PROGRESS",
                "action": "dummy",
                "args": {},
            }

        llm_client.set_mock_backend(infinite_decide)

        loop = AgentLoop(
            goal="Infinite task",
            time_budget=10.0,
            max_steps=4,
            registry=test_registry,
        )
        ok, error, steps = loop.run()
        self.assertFalse(ok)
        self.assertIn("max steps", error.lower())
        self.assertEqual(len(steps), 4)


if __name__ == "__main__":
    unittest.main()
