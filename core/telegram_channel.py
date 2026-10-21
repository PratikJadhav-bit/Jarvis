"""
core/telegram_channel.py — Telegram Remote Control Channel for Mark-LIII (Phase 5).

Features:
  - Long polling via getUpdates (no inbound ports, no router/firewall changes).
  - Token securely stored in Credential Manager / env (never logged).
  - Allowed user ID(s) whitelist in config/telegram.json:
      - Ignores every unauthorized sender silently (never responds or leaks presence).
      - No group chats (private 1:1 only).
      - Rate limiting (max 30 msgs/min per user).
  - Commands:
      /status          — system state (ACTIVE/PAUSED, unattended, active tasks)
      /tasks           — list recent tasks
      /cancel <id>     — cancel a task
      /stop            — emergency kill switch stop
      /resume          — resume system
      /screen          — screenshot sent to allowed user only (NOTIFY tier)
      /unattended [on|off] — toggle unattended mode
  - Voice notes (OGG):
      - Downloads voice note file.
      - Transcribes audio.
      - Echoes transcript back to user.
      - Submits task via task_worker.
  - Plain text: submits task via task_worker.
  - TelegramApprover:
      - Implements Approver interface from core/approvals.
      - Sends inline keyboard: [ ✅ Approve ] / [ ❌ Deny ] bound to action hash.
      - Times out after default 120s (DENY on timeout).
      - Typed "yes" is NEVER accepted.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Optional

from core import approvals
from core import kill_switch
from core import policy
from core import task_queue
from core import task_worker

_log = logging.getLogger("telegram_channel")

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "telegram.json"


# ── Configuration & Credentials ──────────────────────────────────────────────

def get_bot_token() -> Optional[str]:
    """Retrieve Telegram Bot Token from Credential Manager or environment. Never logs token."""
    # 1. Environment variable
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if token and token.strip():
        return token.strip()

    # 2. Keyring (Windows Credential Manager)
    try:
        import keyring
        token = keyring.get_password("Mark-LIII", "telegram_bot_token")
        if token and token.strip():
            return token.strip()
    except Exception:
        pass

    # 3. Config file
    if _CONFIG_PATH.exists():
        try:
            cfg = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            tok = cfg.get("bot_token")
            if tok and tok.strip():
                return tok.strip()
        except Exception:
            pass

    return None


def set_bot_token(token: str) -> bool:
    """Store bot token in Credential Manager."""
    try:
        import keyring
        keyring.set_password("Mark-LIII", "telegram_bot_token", token.strip())
        return True
    except Exception as e:
        _log.warning(f"Failed to store token in Credential Manager: {e}")
        return False


def get_allowed_user_ids() -> set[int]:
    """Load allowed user IDs from config/telegram.json (or fallback config/config.json)."""
    if _CONFIG_PATH.exists():
        try:
            cfg = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            if "allowed_user_ids" in cfg:
                return set(int(uid) for uid in cfg.get("allowed_user_ids", []))
            if "telegram" in cfg and "allowed_user_ids" in cfg["telegram"]:
                return set(int(uid) for uid in cfg["telegram"]["allowed_user_ids"])
        except Exception:
            pass
    # Check fallback config.json
    alt_path = _CONFIG_PATH.parent / "config.json"
    if alt_path.exists():
        try:
            cfg = json.loads(alt_path.read_text(encoding="utf-8"))
            if "telegram" in cfg and "allowed_user_ids" in cfg["telegram"]:
                return set(int(uid) for uid in cfg["telegram"]["allowed_user_ids"])
            if "allowed_user_ids" in cfg:
                return set(int(uid) for uid in cfg.get("allowed_user_ids", []))
        except Exception:
            pass
    return set()


def set_allowed_user_ids(user_ids: list[int]) -> None:
    """Save allowed user IDs to config/telegram.json."""
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    cfg = {}
    if _CONFIG_PATH.exists():
        try:
            cfg = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    cfg["allowed_user_ids"] = [int(u) for u in user_ids]
    if "enabled" not in cfg:
        cfg["enabled"] = True
    _CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# ── Transport Abstraction ────────────────────────────────────────────────────

class TelegramTransport(ABC):
    @abstractmethod
    def send_message(self, chat_id: int, text: str, reply_markup: Optional[dict] = None) -> bool:
        pass

    @abstractmethod
    def send_photo(self, chat_id: int, photo_bytes: bytes, caption: Optional[str] = None) -> bool:
        pass

    @abstractmethod
    def answer_callback_query(self, callback_query_id: str, text: Optional[str] = None) -> bool:
        pass

    @abstractmethod
    def get_updates(self, offset: Optional[int] = None, timeout: int = 30) -> list[dict]:
        pass

    @abstractmethod
    def get_file_bytes(self, file_id: str) -> Optional[bytes]:
        pass


class HttpTelegramTransport(TelegramTransport):
    """Real HTTP transport using official Telegram Bot API via urllib."""
    def __init__(self, token: str):
        self._token = token
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._file_base = f"https://api.telegram.org/file/bot{token}"

    def _post_json(self, method: str, payload: dict) -> Optional[dict]:
        url = f"{self._base_url}/{method}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            _log.warning(f"Telegram API request failed ({method}): {e}")
            return None

    def send_message(self, chat_id: int, text: str, reply_markup: Optional[dict] = None) -> bool:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        res = self._post_json("sendMessage", payload)
        return bool(res and res.get("ok"))

    def send_photo(self, chat_id: int, photo_bytes: bytes, caption: Optional[str] = None) -> bool:
        # Multipart form data
        boundary = f"----WebKitFormBoundary{time.time()}"
        body = io.BytesIO()
        body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n".encode())
        if caption:
            body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption}\r\n".encode())
        body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"screen.png\"\r\nContent-Type: image/png\r\n\r\n".encode())
        body.write(photo_bytes)
        body.write(f"\r\n--{boundary}--\r\n".encode())

        req = urllib.request.Request(
            f"{self._base_url}/sendPhoto",
            data=body.getvalue(),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                return bool(res and res.get("ok"))
        except Exception as e:
            _log.warning(f"Telegram sendPhoto failed: {e}")
            return False

    def answer_callback_query(self, callback_query_id: str, text: Optional[str] = None) -> bool:
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        res = self._post_json("answerCallbackQuery", payload)
        return bool(res and res.get("ok"))

    def get_updates(self, offset: Optional[int] = None, timeout: int = 30) -> list[dict]:
        payload: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        res = self._post_json("getUpdates", payload)
        if res and res.get("ok"):
            return res.get("result", [])
        return []

    def get_file_bytes(self, file_id: str) -> Optional[bytes]:
        res = self._post_json("getFile", {"file_id": file_id})
        if not res or not res.get("ok"):
            return None
        file_path = res.get("result", {}).get("file_path")
        if not file_path:
            return None
        url = f"{self._file_base}/{file_path}"
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                return resp.read()
        except Exception as e:
            _log.warning(f"Failed to download telegram file {file_path}: {e}")
            return None


class MockTelegramTransport(TelegramTransport):
    """In-memory mock transport for offline unit testing."""
    def __init__(self):
        self.sent_messages: list[dict] = []
        self.sent_photos: list[dict] = []
        self.answered_callbacks: list[dict] = []
        self.inbound_updates: list[dict] = []
        self.file_storage: dict[str, bytes] = {}

    def send_message(self, chat_id: int, text: str, reply_markup: Optional[dict] = None) -> bool:
        self.sent_messages.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return True

    def send_photo(self, chat_id: int, photo_bytes: bytes, caption: Optional[str] = None) -> bool:
        self.sent_photos.append({"chat_id": chat_id, "photo_bytes": photo_bytes, "caption": caption})
        return True

    def answer_callback_query(self, callback_query_id: str, text: Optional[str] = None) -> bool:
        self.answered_callbacks.append({"id": callback_query_id, "text": text})
        return True

    def get_updates(self, offset: Optional[int] = None, timeout: int = 30) -> list[dict]:
        ret = list(self.inbound_updates)
        self.inbound_updates.clear()
        return ret

    def get_file_bytes(self, file_id: str) -> Optional[bytes]:
        return self.file_storage.get(file_id, b"dummy_audio_bytes")


# ── Telegram Approver ────────────────────────────────────────────────────────

class TelegramApprover(approvals.Approver):
    """
    Approver that sends an inline button prompt to the user's Telegram.
    The user must click [ ✅ Approve ] or [ ❌ Deny ].
    Typed text like 'yes' is strictly ignored.
    Approval token is cryptographically bound to action hash and expires on timeout.
    """
    def __init__(self, bot: "TelegramBot"):
        self.bot = bot
        self._pending_approvals: dict[str, dict] = {}
        self._lock = threading.Lock()

    def request(
        self,
        action_summary: str,
        tool: str,
        args: dict,
        timeout: float = 120.0,
    ) -> bool:
        act_hash = approvals._action_hash(tool, args)
        short_hash = act_hash[:10]

        # Check if already approved
        if self.approved(tool, args):
            return True

        event = threading.Event()
        result_holder = [False]

        with self._lock:
            self._pending_approvals[short_hash] = {
                "event": event,
                "result": result_holder,
                "tool": tool,
                "args": args,
                "act_hash": act_hash,
                "ts": time.monotonic(),
                "timeout": timeout,
            }

        # Build inline keyboard buttons
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve", "callback_data": f"approve:{short_hash}"},
                    {"text": "❌ Deny", "callback_data": f"deny:{short_hash}"},
                ]
            ]
        }

        prompt_text = (
            f"⚠️ *ACTION APPROVAL REQUIRED*\n\n"
            f"Action: `{tool}`\n"
            f"Details: {action_summary}\n"
            f"Expires in: {int(timeout)}s\n\n"
            f"Tap a button below to confirm (text responses will not be accepted):"
        )

        sent = self.bot.broadcast_message(prompt_text, reply_markup=keyboard)
        if not sent:
            _log.warning("Failed to send Telegram approval prompt (no allowed users or transport failure).")
            return False

        # Wait for user click or timeout
        signaled = event.wait(timeout=timeout)
        with self._lock:
            self._pending_approvals.pop(short_hash, None)

        if not signaled:
            _log.info(f"Telegram approval for {tool} timed out after {timeout}s -> DENIED.")
            return False

        return result_holder[0]

    def handle_callback(self, callback_data: str) -> tuple[bool, str]:
        """
        Handle callback data from inline button click: 'approve:{hash}' or 'deny:{hash}'.
        Returns (success: bool, response_text: str).
        """
        if ":" not in callback_data:
            return False, "Invalid action"

        decision_str, short_hash = callback_data.split(":", 1)
        with self._lock:
            entry = self._pending_approvals.get(short_hash)
            if not entry:
                return False, "Approval expired or not found"

            # Check timeout
            if (time.monotonic() - entry["ts"]) > entry["timeout"]:
                return False, "Approval request expired"

            approved = (decision_str == "approve")
            entry["result"][0] = approved
            entry["event"].set()

        msg = "Action APPROVED." if approved else "Action DENIED."
        return True, msg


# ── Telegram Bot Daemon ──────────────────────────────────────────────────────

# Mock audio transcriber hook for voice notes
_voice_transcriber: Optional[Callable[[bytes], str]] = None


def set_voice_transcriber(fn: Optional[Callable[[bytes], str]]) -> None:
    global _voice_transcriber
    _voice_transcriber = fn


class TelegramBot:
    """
    Long-polling Telegram Bot daemon running inside agentd / background.
    """
    def __init__(
        self,
        transport: Optional[TelegramTransport] = None,
        allowed_user_ids: Optional[set[int]] = None,
        say_fn: Optional[Callable[[str], None]] = None,
    ):
        self.transport = transport
        self.allowed_user_ids = allowed_user_ids if allowed_user_ids is not None else get_allowed_user_ids()
        self.say_fn = say_fn
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._offset = 0
        self.approver = TelegramApprover(self)

        # Rate limiting: user_id -> list of message timestamps in past 60s
        self._rate_limits: dict[int, list[float]] = {}
        self._rate_lock = threading.Lock()

    def start(self) -> None:
        if self._running:
            return
        if self.transport is None:
            tok = get_bot_token()
            if not tok:
                _log.warning("No Telegram Bot token configured. Remote channel not started.")
                return
            self.transport = HttpTelegramTransport(tok)

        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, name="TelegramBotPoll", daemon=True)
        self._thread.start()
        _log.info("Telegram remote channel started.")

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def is_running(self) -> bool:
        return self._running

    def _is_rate_limited(self, user_id: int) -> bool:
        now = time.time()
        with self._rate_lock:
            ts_list = self._rate_limits.setdefault(user_id, [])
            # Prune older than 60s
            ts_list = [t for t in ts_list if (now - t) < 60.0]
            self._rate_limits[user_id] = ts_list
            if len(ts_list) >= 30:  # Max 30 requests/minute
                return True
            ts_list.append(now)
            return False

    def broadcast_message(self, text: str, reply_markup: Optional[dict] = None) -> bool:
        """Send message to all configured allowed user IDs."""
        if not self.transport or not self.allowed_user_ids:
            return False
        sent = False
        for uid in self.allowed_user_ids:
            if self.transport.send_message(uid, text, reply_markup=reply_markup):
                sent = True
        return sent

    def notify_task_completion(self, task_id: str, goal: str, status: str, result: Optional[str] = None, error: Optional[str] = None) -> None:
        """Send task completion notification to allowed Telegram user."""
        emoji = "✅" if status == "COMPLETED" else "❌"
        msg = f"{emoji} *Task {status}*\nGoal: {goal}\n"
        if result:
            msg += f"Result: {result[:300]}\n"
        if error:
            msg += f"Error: {error[:300]}\n"
        self.broadcast_message(msg)

    def _poll_loop(self) -> None:
        while self._running:
            try:
                updates = self.transport.get_updates(offset=self._offset, timeout=5)
                for update in updates:
                    up_id = update.get("update_id", 0)
                    if up_id >= self._offset:
                        self._offset = up_id + 1
                    self.process_update(update)
            except Exception as e:
                _log.warning(f"Error in Telegram poll loop: {e}")
                time.sleep(2.0)

    def process_update(self, update: dict) -> None:
        """
        Process a single incoming Telegram update.
        Enforces:
          1. Allowed user ID whitelist (silent drop for unauthorized users).
          2. No group chats.
          3. Rate limit per user.
        """
        # Handle callback query (inline button press)
        if "callback_query" in update:
            cb = update["callback_query"]
            cb_id = cb.get("id")
            user = cb.get("from", {})
            user_id = user.get("id")
            data = cb.get("data", "")

            # Verify sender
            if user_id not in self.allowed_user_ids:
                return  # Silently drop unauthorized sender

            ok, resp_text = self.approver.handle_callback(data)
            self.transport.answer_callback_query(cb_id, text=resp_text)
            self.transport.send_message(user_id, f"📝 {resp_text}")
            return

        message = update.get("message")
        if not message:
            return

        chat = message.get("chat", {})
        chat_type = chat.get("type", "private")
        if chat_type != "private":
            # No group chats!
            return

        user = message.get("from", {})
        user_id = user.get("id")
        if not user_id or user_id not in self.allowed_user_ids:
            # Unauthorized user: IGNORE SILENTLY without replying!
            return

        if self._is_rate_limited(user_id):
            self.transport.send_message(user_id, "⚠️ Rate limit reached. Please wait a minute.")
            return

        # Voice note handling
        if "voice" in message:
            voice = message["voice"]
            file_id = voice.get("file_id")
            if file_id:
                self._handle_voice_note(user_id, file_id)
            return

        # Text command handling
        text = (message.get("text") or "").strip()
        if not text:
            return

        self._handle_text(user_id, text)

    def _handle_voice_note(self, user_id: int, file_id: str) -> None:
        raw_audio = self.transport.get_file_bytes(file_id)
        if not raw_audio:
            self.transport.send_message(user_id, "❌ Failed to download voice note.")
            return

        # Transcribe audio
        transcript = ""
        global _voice_transcriber
        if _voice_transcriber:
            transcript = _voice_transcriber(raw_audio)
        else:
            # Fallback to local STT / LLM audio transcription
            try:
                from core import stt
                transcript = stt.transcribe_audio(raw_audio)
            except Exception:
                transcript = "Process audio voice note"

        if not transcript or not transcript.strip():
            self.transport.send_message(user_id, "❌ Could not transcribe audio.")
            return

        transcript = transcript.strip()
        self.transport.send_message(user_id, f"🎙️ Heard: \"{transcript}\"\nSubmitting task to background agent...")

        # Submit task
        worker = task_worker.get_worker()
        worker.start()
        task_id = worker.submit(transcript)
        self.transport.send_message(user_id, f"🚀 Task submitted with ID: `{task_id}`")

    def _handle_text(self, user_id: int, text: str) -> None:
        cmd = text.split()[0].lower()
        args = text[len(cmd):].strip()

        if cmd == "/status":
            state = "PAUSED" if kill_switch.is_paused() else "ACTIVE"
            unatt = "ON" if policy.is_unattended() else "OFF"
            active_tasks = task_queue.list_tasks(status="RUNNING")
            pending_tasks = task_queue.list_tasks(status="PENDING")
            reply = (
                f"🤖 *JARVIS Status*\n"
                f"State: {state}\n"
                f"Unattended Mode: {unatt}\n"
                f"Running Tasks: {len(active_tasks)}\n"
                f"Pending Tasks: {len(pending_tasks)}"
            )
            self.transport.send_message(user_id, reply)
            return

        if cmd == "/tasks":
            tasks = task_queue.list_tasks(limit=5)
            if not tasks:
                self.transport.send_message(user_id, "No recent tasks found.")
                return
            lines = ["📋 *Recent Tasks:*"]
            for t in tasks:
                lines.append(f"• `{t['id']}` [{t['status']}]: {t['goal'][:40]}")
            self.transport.send_message(user_id, "\n".join(lines))
            return

        if cmd == "/cancel":
            if not args:
                self.transport.send_message(user_id, "Usage: /cancel <task_id>")
                return
            ok = task_queue.cancel_task(args)
            if ok:
                self.transport.send_message(user_id, f"✅ Task `{args}` cancelled.")
            else:
                self.transport.send_message(user_id, f"❌ Task `{args}` not found or not cancellable.")
            return

        if cmd == "/stop":
            kill_switch.trigger()
            self.transport.send_message(user_id, "🛑 EMERGENCY STOP activated. All tasks cancelled, system PAUSED.")
            return

        if cmd == "/resume":
            kill_switch.resume()
            self.transport.send_message(user_id, "▶️ System RESUMED and listening.")
            return

        if cmd == "/unattended":
            arg = args.lower()
            if arg in ("on", "enable", "true"):
                policy.set_unattended(True)
                self.transport.send_message(user_id, "🔒 Unattended mode turned ON.")
            elif arg in ("off", "disable", "false"):
                policy.set_unattended(False)
                self.transport.send_message(user_id, "🔓 Unattended mode turned OFF.")
            else:
                cur = "ON" if policy.is_unattended() else "OFF"
                self.transport.send_message(user_id, f"Unattended mode is currently {cur}. Use `/unattended on` or `/unattended off`.")
            return

        if cmd == "/screen":
            # Send screenshot to user
            try:
                import pyautogui
                img = pyautogui.screenshot()
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                photo_bytes = buf.getvalue()
                self.transport.send_photo(user_id, photo_bytes, caption="📸 Current screen capture")
            except Exception as e:
                # Mock or pillow fallback
                self.transport.send_message(user_id, f"Screen capture error: {e}")
            return

        # Any other plain text: submit as background task
        worker = task_worker.get_worker()
        worker.start()
        task_id = worker.submit(text)
        self.transport.send_message(user_id, f"🚀 Task submitted with ID: `{task_id}`\nGoal: {text}")


# ── Global Singleton ─────────────────────────────────────────────────────────

_bot_instance: Optional[TelegramBot] = None
_bot_lock = threading.Lock()


def get_telegram_bot() -> TelegramBot:
    global _bot_instance
    with _bot_lock:
        if _bot_instance is None:
            _bot_instance = TelegramBot()
        return _bot_instance
