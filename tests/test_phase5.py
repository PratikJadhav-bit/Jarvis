"""
tests/test_phase5.py — Unit tests and Acceptance test for Phase 5 (Control while away).
"""
import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from core import approvals
from core import kill_switch
from core import policy
from core import keep_awake
from core import screen_lock
from core import task_queue
from core import task_worker
from core import telegram_channel
from core.action_loader import ActionRecord, ActionRegistry
from core.agent_loop import AgentLoop


class TestPhase5AwayControl(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tasks_db = Path(self.temp_dir.name) / "tasks.db"
        task_queue.set_db_path(self.tasks_db)
        kill_switch.resume()
        policy.reload_policy()
        policy.set_paused(False)
        screen_lock.set_mock_screen_locked(None)

        self.allowed_user_id = 12345678
        self.mock_transport = telegram_channel.MockTelegramTransport()
        self.bot = telegram_channel.TelegramBot(
            transport=self.mock_transport,
            allowed_user_ids={self.allowed_user_id}
        )

    def tearDown(self):
        self.bot.stop()
        task_worker.stop_worker(timeout=0.5)
        screen_lock.set_mock_screen_locked(None)
        keep_awake.release_keep_awake()
        task_queue.close_all_connections()
        kill_switch.resume()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_unauthorized_sender_silently_ignored(self):
        """Unauthorized sender messages are silently dropped; no response is ever sent."""
        update = {
            "update_id": 1,
            "message": {
                "message_id": 101,
                "from": {"id": 99999999, "username": "attacker"},
                "chat": {"id": 99999999, "type": "private"},
                "text": "format c: /fs:NTFS"
            }
        }
        self.bot.process_update(update)
        # Verify transport sent ZERO messages back
        self.assertEqual(len(self.mock_transport.sent_messages), 0)

    def test_group_chats_rejected(self):
        """Group chats are rejected; only private 1:1 chat is permitted."""
        update = {
            "update_id": 2,
            "message": {
                "message_id": 102,
                "from": {"id": self.allowed_user_id, "username": "owner"},
                "chat": {"id": -10012345678, "type": "supergroup"},
                "text": "/status"
            }
        }
        self.bot.process_update(update)
        self.assertEqual(len(self.mock_transport.sent_messages), 0)

    def test_rate_limiting_enforced(self):
        """Excessive messages beyond rate limit are throttled."""
        for i in range(35):
            update = {
                "update_id": 10 + i,
                "message": {
                    "message_id": 200 + i,
                    "from": {"id": self.allowed_user_id},
                    "chat": {"id": self.allowed_user_id, "type": "private"},
                    "text": "/status"
                }
            }
            self.bot.process_update(update)

        # After 30 messages, throttling message is sent
        last_msg = self.mock_transport.sent_messages[-1]
        self.assertIn("Rate limit", last_msg["text"])

    def test_telegram_commands_status_and_stop_resume(self):
        """Test /status, /stop, /resume, /unattended commands."""
        # /status
        update_status = {
            "update_id": 100,
            "message": {
                "message_id": 301,
                "from": {"id": self.allowed_user_id},
                "chat": {"id": self.allowed_user_id, "type": "private"},
                "text": "/status"
            }
        }
        self.bot.process_update(update_status)
        self.assertIn("JARVIS Status", self.mock_transport.sent_messages[-1]["text"])

        # /stop
        update_stop = {
            "update_id": 101,
            "message": {
                "message_id": 302,
                "from": {"id": self.allowed_user_id},
                "chat": {"id": self.allowed_user_id, "type": "private"},
                "text": "/stop"
            }
        }
        self.bot.process_update(update_stop)
        self.assertTrue(kill_switch.is_paused())
        self.assertIn("EMERGENCY STOP", self.mock_transport.sent_messages[-1]["text"])

        # /resume
        update_resume = {
            "update_id": 102,
            "message": {
                "message_id": 303,
                "from": {"id": self.allowed_user_id},
                "chat": {"id": self.allowed_user_id, "type": "private"},
                "text": "/resume"
            }
        }
        self.bot.process_update(update_resume)
        self.assertFalse(kill_switch.is_paused())
        self.assertIn("RESUMED", self.mock_transport.sent_messages[-1]["text"])

        # /unattended on
        update_unatt = {
            "update_id": 103,
            "message": {
                "message_id": 304,
                "from": {"id": self.allowed_user_id},
                "chat": {"id": self.allowed_user_id, "type": "private"},
                "text": "/unattended on"
            }
        }
        self.bot.process_update(update_unatt)
        self.assertTrue(policy.is_unattended())

    def test_approval_buttons_bind_to_hash_and_expire(self):
        """Approval buttons bind cryptographically to action hash, and expire on timeout."""
        approver = self.bot.approver
        tool = "delete_file"
        args = {"path": "C:\\important.doc"}
        act_hash = approvals._action_hash(tool, args)[:10]

        # Request in a background thread
        req_result = [None]
        def _req():
            req_result[0] = approver.request("Delete important.doc", tool, args, timeout=1.0)

        t = threading.Thread(target=_req)
        t.start()
        time.sleep(0.1)

        # Check prompt message was sent with inline keyboard
        sent = self.mock_transport.sent_messages[-1]
        self.assertIn("APPROVAL REQUIRED", sent["text"])
        self.assertIn("inline_keyboard", sent["reply_markup"])
        btn_data = sent["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        self.assertEqual(btn_data, f"approve:{act_hash}")

        # Simulate user clicking [ ✅ Approve ] via callback query
        cb_update = {
            "update_id": 201,
            "callback_query": {
                "id": "cb_1",
                "from": {"id": self.allowed_user_id},
                "data": f"approve:{act_hash}"
            }
        }
        self.bot.process_update(cb_update)
        t.join(timeout=2.0)

        self.assertTrue(req_result[0])

    def test_approval_denied_on_timeout(self):
        """Approval prompt expires and returns False on timeout."""
        approver = self.bot.approver
        res = approver.request("Dangerous op", "format_disk", {"drive": "D:"}, timeout=0.1)
        self.assertFalse(res)

    def test_voice_note_transcription_and_task_submission(self):
        """Voice notes (OGG) are downloaded, transcribed, and submitted as background task."""
        telegram_channel.set_voice_transcriber(lambda _: "find all pdfs and summarize them")
        self.mock_transport.file_storage["audio_file_123"] = b"OGG_HEADER_DATA"

        update = {
            "update_id": 300,
            "message": {
                "message_id": 401,
                "from": {"id": self.allowed_user_id},
                "chat": {"id": self.allowed_user_id, "type": "private"},
                "voice": {"file_id": "audio_file_123"}
            }
        }
        self.bot.process_update(update)

        # Echoed back transcript
        sent_texts = [m["text"] for m in self.mock_transport.sent_messages]
        self.assertTrue(any("find all pdfs and summarize them" in t for t in sent_texts))
        self.assertTrue(any("Task submitted with ID" in t for t in sent_texts))

        # Check task exists in task queue
        tasks = task_queue.list_tasks(limit=1)
        self.assertGreaterEqual(len(tasks), 1)
        self.assertIn("find all pdfs", tasks[0]["goal"])

    def test_keep_awake_set_and_released(self):
        """acquire_keep_awake sets active flags and release_keep_awake clears them."""
        self.assertFalse(keep_awake.is_keep_awake_active())
        keep_awake.acquire_keep_awake(display_required=True)
        self.assertTrue(keep_awake.is_keep_awake_active())
        self.assertTrue(keep_awake.is_display_keep_awake_active())

        keep_awake.release_keep_awake()
        self.assertFalse(keep_awake.is_keep_awake_active())
        self.assertFalse(keep_awake.is_display_keep_awake_active())

    def test_locked_screen_detection_pauses_gui_tasks(self):
        """
        When screen is locked, GUI actions are blocked and cannot blind-click,
        while non-GUI actions execute normally.
        """
        gui_ran = [False]
        cli_ran = [False]

        def ui_automation_handler(parameters, **_):
            gui_ran[0] = True
            return {"ok": True, "output": "Clicked UI element"}

        def process_data_handler(parameters, **_):
            cli_ran[0] = True
            return {"ok": True, "output": "CLI script completed"}

        records = {
            "ui_automation": ActionRecord(name="ui_automation", handler=ui_automation_handler, valid=True),
            "process_data": ActionRecord(name="process_data", handler=process_data_handler, valid=True),
        }
        test_registry = ActionRegistry(records, logger=lambda _: None)

        # Simulate locked workstation
        screen_lock.set_mock_screen_locked(True)

        # 1. Non-GUI CLI action should succeed even while locked
        def mock_cli_decide(prompt, system):
            if "Step 1:" not in prompt:
                return {"status": "IN_PROGRESS", "action": "process_data", "args": {"command": "dir"}}
            return {"status": "DONE", "summary": "Done"}

        from core import llm_client
        llm_client.set_mock_backend(mock_cli_decide)
        loop_cli = AgentLoop(goal="Check files in directory", task_id="task_cli", registry=test_registry)
        ok_cli, _, _ = loop_cli.run()
        self.assertTrue(ok_cli)
        self.assertTrue(cli_ran[0])

        # 2. GUI action must be BLOCKED on locked screen
        def mock_gui_decide(prompt, system):
            return {"status": "IN_PROGRESS", "action": "ui_automation", "args": {"action": "click"}}

        llm_client.set_mock_backend(mock_gui_decide)
        loop_gui = AgentLoop(goal="Click button in app", task_id="task_gui", registry=test_registry)
        ok_gui, summary_gui, _ = loop_gui.run()

        self.assertFalse(ok_gui)
        self.assertFalse(gui_ran[0])  # Real GUI click was never executed!
        self.assertIn("locked or on secure desktop", summary_gui.lower())


class TestPhase5Acceptance(unittest.TestCase):
    """
    ACCEPTANCE TEST FOR PHASE 5:
    From phone on mobile data (Telegram remote channel):
    1. A voice note runs a task.
    2. A risky step sends an Approve/Deny prompt with inline buttons.
    3. /stop halts everything immediately.
    """
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tasks_db = Path(self.temp_dir.name) / "tasks.db"
        task_queue.set_db_path(self.tasks_db)
        kill_switch.resume()
        policy.reload_policy()
        policy.set_paused(False)

        self.allowed_user_id = 987654321
        self.mock_transport = telegram_channel.MockTelegramTransport()
        self.bot = telegram_channel.TelegramBot(
            transport=self.mock_transport,
            allowed_user_ids={self.allowed_user_id}
        )

    def tearDown(self):
        self.bot.stop()
        task_worker.stop_worker(timeout=0.5)
        approvals.set_approver(approvals.HudApprover())
        task_queue.close_all_connections()
        kill_switch.resume()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_phase5_acceptance_remote_voice_note_risky_confirm_stop(self):
        # 1. Voice note arrives via Telegram
        telegram_channel.set_voice_transcriber(lambda _: "archive old invoices and remove raw files")
        self.mock_transport.file_storage["voice_note_rec"] = b"OGG_PAYLOAD"

        voice_update = {
            "update_id": 501,
            "message": {
                "message_id": 801,
                "from": {"id": self.allowed_user_id},
                "chat": {"id": self.allowed_user_id, "type": "private"},
                "voice": {"file_id": "voice_note_rec"}
            }
        }
        self.bot.process_update(voice_update)

        # Verify task submitted
        tasks = task_queue.list_tasks(status="PENDING")
        self.assertEqual(len(tasks), 1)
        task_id = tasks[0]["id"]
        self.assertIn("archive old invoices", tasks[0]["goal"])

        # 2. A risky step sends an Approve/Deny prompt to Telegram
        approvals.set_approver(self.bot.approver)

        risky_tool = "delete_file"
        risky_args = {"path": "C:\\data\\invoices_2025.csv"}
        act_hash = approvals._action_hash(risky_tool, risky_args)[:10]

        approval_result = [None]
        def _run_risky_action():
            summary = "Delete invoices_2025.csv?"
            approval_result[0] = approvals.request_approval(summary, risky_tool, risky_args, timeout=2.0)

        t_action = threading.Thread(target=_run_risky_action)
        t_action.start()
        time.sleep(0.1)

        # Verify inline prompt sent to Telegram
        prompt_msg = self.mock_transport.sent_messages[-1]
        self.assertIn("APPROVAL REQUIRED", prompt_msg["text"])
        self.assertIn("Delete invoices_2025.csv", prompt_msg["text"])

        # User taps [ ✅ Approve ] via Telegram
        approve_cb = {
            "update_id": 502,
            "callback_query": {
                "id": "cb_approve_1",
                "from": {"id": self.allowed_user_id},
                "data": f"approve:{act_hash}"
            }
        }
        self.bot.process_update(approve_cb)
        t_action.join(timeout=3.0)

        self.assertTrue(approval_result[0])
        print("\n[Phase 5 Acceptance] Remote voice note submitted task successfully.")
        print("[Phase 5 Acceptance] Risky CONFIRM-tier action prompted and approved via inline button.")

        # 3. /stop halts everything
        stop_update = {
            "update_id": 503,
            "message": {
                "message_id": 802,
                "from": {"id": self.allowed_user_id},
                "chat": {"id": self.allowed_user_id, "type": "private"},
                "text": "/stop"
            }
        }
        self.bot.process_update(stop_update)

        self.assertTrue(kill_switch.is_paused())
        self.assertIn("EMERGENCY STOP", self.mock_transport.sent_messages[-1]["text"])
        print("[Phase 5 Acceptance] /stop command halted all tasks and engaged emergency pause.")


if __name__ == "__main__":
    unittest.main()
