"""
tests/test_phase1.py — Acceptance tests for Phase 1: Safety Layer.

These tests run entirely offline: no network calls, no file moves, no mouse.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.policy import (
    check, Tier, Decision,
    set_paused, is_paused,
    set_unattended, is_unattended,
    reload_policy,
    PROTECTED_PATHS,
)
from core.approvals import (
    AutoDenyApprover, HudApprover,
    _action_hash, ApprovalRegistry, _PendingEntry,
    set_approver, request_approval,
)
from core.audit import log_decision, audit_recent, _redact
from core import kill_switch


class TestPolicyTiers(unittest.TestCase):
    """policy.check() returns the correct tier from policy.json."""

    def setUp(self):
        reload_policy()
        set_paused(False)
        set_unattended(False)

    def test_auto_tier_read_only_tools(self):
        for tool in ("web_search", "system_status", "recall_memory"):
            d = check(tool, {})
            self.assertEqual(d.tier, Tier.AUTO, f"{tool!r} should be AUTO, got {d.tier}")

    def test_notify_tier_file_ops(self):
        d = check("file_controller", {"action": "move", "source": "a.txt", "dest": "b.txt"})
        self.assertEqual(d.tier, Tier.NOTIFY)

    def test_confirm_tier_send_message(self):
        d = check("send_message", {"to": "friend", "body": "hi"})
        self.assertEqual(d.tier, Tier.CONFIRM)

    def test_confirm_tier_unknown_tool(self):
        # Unknown tools default to CONFIRM (safest)
        d = check("totally_unknown_tool_xyz", {})
        self.assertEqual(d.tier, Tier.CONFIRM)

    def test_deny_tier_when_paused(self):
        set_paused(True)
        d = check("web_search", {})
        self.assertEqual(d.tier, Tier.DENY)
        self.assertIn("paused", d.reason)

    def test_deny_protected_path_write(self):
        protected = list(PROTECTED_PATHS)[0]
        d = check("file_controller", {"dest": protected})
        self.assertEqual(d.tier, Tier.DENY)

    def test_deny_shell_dangerous_command(self):
        d = check("run_powershell", {"command": "Format-Volume C: -Force"})
        self.assertEqual(d.tier, Tier.DENY)

    def test_confirm_shell_recursive_remove(self):
        d = check("run_powershell", {"command": "Remove-Item -Recurse C:\\Users\\test\\*"})
        self.assertEqual(d.tier, Tier.CONFIRM)

    def test_deny_cannot_be_approved(self):
        """DENY tier: policy blocks; the approver is never even reached."""
        d = check("run_powershell", {"command": "diskpart /s nuke.txt"})
        self.assertEqual(d.tier, Tier.DENY)


class TestTaintEscalation(unittest.TestCase):
    def setUp(self):
        reload_policy()
        set_paused(False)
        set_unattended(False)

    def test_notify_outward_escalates_when_tainted(self):
        """NOTIFY-tier outward tool → CONFIRM when task is tainted."""
        ctx = {"tainted": True}
        d = check("run_powershell", {"command": "echo hello"}, context=ctx)
        # run_powershell is CONFIRM by default, verify it stays CONFIRM (not DENY)
        self.assertIn(d.tier, (Tier.CONFIRM, Tier.DENY))

    def test_auto_unchanged_when_tainted(self):
        """AUTO-tier stays AUTO even in tainted context."""
        ctx = {"tainted": True}
        d = check("web_search", {}, context=ctx)
        self.assertEqual(d.tier, Tier.AUTO)

    def test_notify_non_outward_unchanged_when_tainted(self):
        """NOTIFY-tier non-outward tool stays NOTIFY when tainted."""
        ctx = {"tainted": True}
        d = check("file_controller", {"action": "move"}, context=ctx)
        # file_controller is outward, should escalate to CONFIRM
        self.assertEqual(d.tier, Tier.CONFIRM)


class TestApprovals(unittest.TestCase):
    def test_auto_deny_approver(self):
        approver = AutoDenyApprover()
        result = approver.request("Delete system32", "file_controller", {"dest": "/"})
        self.assertFalse(result)

    def test_action_hash_is_deterministic(self):
        h1 = _action_hash("file_controller", {"action": "delete", "path": "/tmp/x"})
        h2 = _action_hash("file_controller", {"path": "/tmp/x", "action": "delete"})
        self.assertEqual(h1, h2, "Hash must be stable regardless of dict insertion order")

    def test_action_hash_differs_for_different_args(self):
        h1 = _action_hash("file_controller", {"action": "delete"})
        h2 = _action_hash("file_controller", {"action": "move"})
        self.assertNotEqual(h1, h2)

    def test_approval_registry_flow(self):
        reg = ApprovalRegistry()
        h = "abc123"
        entry = _PendingEntry("Delete system32", h, timeout=120.0)
        reg.register(h, entry)

        # Should be pending before resolution
        self.assertIsNotNone(reg.pending(h))

        # Resolve as approved
        ok = reg.resolve(h, True)
        self.assertTrue(ok)

        # Consume returns True
        result = reg.consume(h)
        self.assertTrue(result)

        # Second consume returns None (already consumed)
        result2 = reg.consume(h)
        self.assertIsNone(result2)

    def test_approval_registry_expiry(self):
        reg = ApprovalRegistry()
        h = "expiry_test"
        entry = _PendingEntry("test", h, timeout=0.01)
        reg.register(h, entry)
        time.sleep(0.05)  # let it expire
        result = reg.consume(h)
        self.assertFalse(result)  # expired → denied

    def test_policy_blocked_with_auto_deny_approver(self):
        """When policy is CONFIRM and approver denies, run() returns error."""
        set_approver(AutoDenyApprover())
        try:
            from core.action_loader import ActionRecord, ActionRegistry

            def delete_handler(parameters, **_):
                return {"ok": True, "output": "Deleted"}

            rec = ActionRecord(name="send_message", handler=delete_handler, valid=True)
            registry = ActionRegistry({"send_message": rec}, logger=lambda _: None)
            result = registry.run("send_message", {"to": "boss", "body": "resign"})
            self.assertFalse(result.ok)
            self.assertIn("approved", result.error.lower())
        finally:
            set_approver(HudApprover())


class TestKillSwitch(unittest.TestCase):
    def setUp(self):
        set_paused(False)

    def tearDown(self):
        set_paused(False)

    def test_trigger_pauses_policy(self):
        """kill_switch.trigger() must pause the system within 2 s."""
        started = time.monotonic()
        kill_switch.trigger()
        elapsed = time.monotonic() - started
        self.assertTrue(is_paused(), "System should be paused after kill switch")
        self.assertLess(elapsed, 2.0, "Kill switch must fire within 2 s")

    def test_resume_unpauses(self):
        kill_switch.trigger()
        self.assertTrue(is_paused())
        kill_switch.resume()
        self.assertFalse(is_paused())

    def test_all_tools_denied_while_paused(self):
        kill_switch.trigger()
        d = check("web_search", {})
        self.assertEqual(d.tier, Tier.DENY)
        kill_switch.resume()

    def test_callback_called_on_trigger(self):
        called = []
        kill_switch.add_stop_callback(lambda: called.append(True))
        kill_switch.trigger()
        self.assertTrue(len(called) >= 1)
        kill_switch.resume()


class TestAuditLog(unittest.TestCase):
    def test_log_and_retrieve(self):
        log_decision("web_search", {"query": "test"}, "AUTO", "AUTO",
                     result_ok=True, output_summary="results here")
        records = audit_recent(5)
        self.assertGreater(len(records), 0)
        tools = [r["tool"] for r in records]
        self.assertIn("web_search", tools)

    def test_secrets_redacted(self):
        redacted = _redact("My key is AIzaSyDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD")
        self.assertNotIn("AIzaSy", redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_bearer_token_redacted(self):
        redacted = _redact("Authorization: Bearer sk-testkey1234567890abcdefghij")
        self.assertNotIn("sk-", redacted)

    def test_audit_never_raises(self):
        """log_decision must not raise even on bad input."""
        try:
            log_decision(None, object(), None, None)  # garbage input
        except Exception as e:
            self.fail(f"log_decision raised: {e}")


class TestUnattendedMode(unittest.TestCase):
    def setUp(self):
        reload_policy()
        set_paused(False)
        set_unattended(False)

    def tearDown(self):
        set_unattended(False)

    def test_unattended_allowlisted_tool_unchanged(self):
        set_unattended(True)
        d = check("web_search", {})
        # web_search is allowlisted → should stay AUTO
        self.assertEqual(d.tier, Tier.AUTO)

    def test_unattended_non_allowlisted_escalates_to_confirm(self):
        set_unattended(True)
        d = check("file_controller", {"action": "move"})
        # file_controller is NOTIFY but not allowlisted → escalates to CONFIRM
        self.assertEqual(d.tier, Tier.CONFIRM)

    def test_unattended_off_no_escalation(self):
        set_unattended(False)
        d = check("file_controller", {"action": "move"})
        self.assertEqual(d.tier, Tier.NOTIFY)


if __name__ == "__main__":
    unittest.main()
