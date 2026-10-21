"""
Unit tests for Phase 0 Acceptance Criteria:
1. py_compile all files
2. Structured results {ok: bool, output: str, error: str|None}
3. DPI scaling coordinate transformation
4. Centralized model configuration
5. .gitignore safety verification
6. Secrets retrieval and Credential Manager / env integration
"""

import os
import sys
import unittest
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config
from core.result import ActionResult, action_ok, action_error, to_action_result
from actions.universal_control import scale_coordinates
from core.action_loader import ActionRegistry, ActionRecord


class TestStructuredResults(unittest.TestCase):
    def test_action_result_properties(self):
        res = ActionResult(ok=True, output="Operation completed", error=None)
        self.assertTrue(res.ok)
        self.assertEqual(res.output, "Operation completed")
        self.assertIsNone(res.error)
        self.assertEqual(str(res), "Operation completed")
        self.assertTrue(bool(res))

        err = ActionResult(ok=False, output="", error="Something went wrong")
        self.assertFalse(err.ok)
        self.assertEqual(err.error, "Something went wrong")
        self.assertEqual(str(err), "Something went wrong")
        self.assertFalse(bool(err))

    def test_helpers(self):
        ok_res = action_ok("Opened Chrome.")
        self.assertTrue(ok_res.ok)
        self.assertEqual(ok_res.output, "Opened Chrome.")
        self.assertIsNone(ok_res.error)

        err_res = action_error("Failed to open Chrome.")
        self.assertFalse(err_res.ok)
        self.assertEqual(err_res.error, "Failed to open Chrome.")

    def test_to_action_result_normalization(self):
        # Dict with ok
        d = {"ok": True, "output": "Custom output", "error": None}
        r1 = to_action_result(d)
        self.assertIsInstance(r1, ActionResult)
        self.assertTrue(r1.ok)
        self.assertEqual(r1.output, "Custom output")

        # String with success
        r2 = to_action_result("Success message")
        self.assertTrue(r2.ok)
        self.assertEqual(r2.output, "Success message")

        # String with failure prefix
        r3 = to_action_result("Failed to find element")
        self.assertFalse(r3.ok)
        self.assertEqual(r3.error, "Failed to find element")

        # Boolean
        r4 = to_action_result(True)
        self.assertTrue(r4.ok)
        r5 = to_action_result(False)
        self.assertFalse(r5.ok)

    def test_action_loader_structured_return(self):
        # Mock action handler returning raw string
        def mock_handler(parameters):
            return "App launched"

        rec = ActionRecord(name="mock_tool", handler=mock_handler, valid=True)
        registry = ActionRegistry({"mock_tool": rec}, logger=lambda _: None)

        result = registry.run("mock_tool", {})
        self.assertIsInstance(result, ActionResult)
        self.assertTrue(result.ok)
        self.assertEqual(result.output, "App launched")

    def test_action_loader_crash_handling(self):
        def crashing_handler(parameters):
            raise RuntimeError("Hardware disconnect")

        rec = ActionRecord(name="crash_tool", handler=crashing_handler, valid=True)
        registry = ActionRegistry({"crash_tool": rec}, logger=lambda _: None)

        result = registry.run("crash_tool", {})
        self.assertIsInstance(result, ActionResult)
        self.assertFalse(result.ok)
        self.assertIn("Hardware disconnect", result.error)


class TestDpiScaling(unittest.TestCase):
    def test_scale_coordinates_100_percent(self):
        # When image dimensions match screen dimensions exactly
        x, y = scale_coordinates(500, 300, 1920, 1080)
        # Assuming pyautogui size or mock
        import pyautogui
        screen_w, screen_h = pyautogui.size()
        expected_x = int(round(500 * (screen_w / 1920.0)))
        expected_y = int(round(300 * (screen_h / 1080.0)))
        self.assertEqual(x, expected_x)
        self.assertEqual(y, expected_y)

    def test_scale_coordinates_proportional(self):
        # Screenshot is 2880 x 1800 (150% scaled on a 1920 x 1200 logical display)
        # Coordinate at center (1440, 900) should scale to center of screen
        import pyautogui
        screen_w, screen_h = pyautogui.size()
        x, y = scale_coordinates(1440, 900, 2880, 1800)
        self.assertEqual(x, int(round(screen_w * 0.5)))
        self.assertEqual(y, int(round(screen_h * 0.5)))

    def test_scale_coordinates_zero_handling(self):
        x, y = scale_coordinates(100, 200, 0, 0)
        self.assertEqual(x, 100)
        self.assertEqual(y, 200)


class TestCentralizedConfig(unittest.TestCase):
    def test_default_models(self):
        self.assertEqual(config.DEFAULT_MODEL, "gemini-flash-latest")
        self.assertEqual(config.get_model("universal_control"), "gemini-flash-latest")
        self.assertEqual(config.get_model("planner"), "gemini-flash-latest")

    def test_model_override_via_env(self):
        os.environ["GEMINI_MODEL_TEST"] = "gemini-custom-pro"
        self.assertEqual(config.get_model("test"), "gemini-custom-pro")
        del os.environ["GEMINI_MODEL_TEST"]

    def test_gitignore_safety_check(self):
        safe, issues = config.check_gitignore(PROJECT_ROOT)
        self.assertTrue(safe, f"Gitignore check reported issues: {issues}")
        self.assertEqual(len(issues), 0)

    def test_secret_management_env_override(self):
        test_key = "AIzaSyTestKeyFromEnv1234567890"
        os.environ["GEMINI_API_KEY"] = test_key
        key = config.get_api_key()
        self.assertEqual(key, test_key)
        del os.environ["GEMINI_API_KEY"]


if __name__ == "__main__":
    unittest.main()
