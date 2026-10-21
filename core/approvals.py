"""
core/approvals.py — Human-approval interface for Mark-LIII.

CONFIRM-tier actions must pass through an Approver before execution.
The model cannot supply or influence the approval — it arrives through
a separate channel (HUD button, Telegram inline keyboard, etc.).

Approval tokens are bound to a SHA-256 hash of (tool_name + canonical args)
so they cannot be reused for a different action.  They expire after
``timeout`` seconds (default 120); a timeout is treated as a denial.

Design goal: Phase 1 ships HudApprover (reuses the existing confirm banner).
Phase 5 adds TelegramApprover against the same interface without touching
this file.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from abc import ABC, abstractmethod
from typing import Optional


# ── Shared helpers ────────────────────────────────────────────────────────────

def _action_hash(tool: str, args: dict) -> str:
    """Stable SHA-256 fingerprint of tool + args (keys sorted for determinism)."""
    payload = json.dumps({"tool": tool, "args": args}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


# ── Interface ────────────────────────────────────────────────────────────────

class Approver(ABC):
    """
    Abstract approval gateway.  Concrete implementations:
      HudApprover     — Phase 1: PyQt6 HUD confirm banner
      TelegramApprover — Phase 5: Telegram inline keyboard
    """

    @abstractmethod
    def request(
        self,
        action_summary: str,
        tool: str,
        args: dict,
        timeout: float = 120.0,
    ) -> bool:
        """
        Present an approval request and block until:
          - The user explicitly approves  → return True
          - The user denies               → return False
          - ``timeout`` seconds elapse    → return False (denied by timeout)

        The implementation MUST verify that the approval token's hash
        matches ``_action_hash(tool, args)`` — it must not accept a token
        created for a different action.
        """

    def approved(self, tool: str, args: dict) -> bool:
        """
        Non-blocking check: has a valid approval already been granted for
        this exact action hash?  Override when the approver supports
        pre-authorisation (e.g. a Telegram button press that arrived before
        the task reached the confirmation gate).
        """
        return False


# ── Pending-approval registry (shared across approvers) ──────────────────────

class _PendingEntry:
    __slots__ = ("summary", "action_hash", "approved", "ts", "timeout")

    def __init__(self, summary: str, action_hash: str, timeout: float):
        self.summary     = summary
        self.action_hash = action_hash
        self.approved: Optional[bool] = None   # None = pending
        self.ts      = time.monotonic()
        self.timeout = timeout

    def is_expired(self) -> bool:
        return time.monotonic() - self.ts > self.timeout


class ApprovalRegistry:
    """Thread-safe store of in-flight approval requests."""

    def __init__(self):
        self._entries: dict[str, _PendingEntry] = {}   # hash → entry
        self._lock = threading.Lock()

    def pending(self, action_hash: str) -> Optional[_PendingEntry]:
        with self._lock:
            return self._entries.get(action_hash)

    def register(self, action_hash: str, entry: _PendingEntry) -> None:
        with self._lock:
            self._entries[action_hash] = entry

    def resolve(self, action_hash: str, approved: bool) -> bool:
        """Mark an entry as approved/denied. Returns False if not found or expired."""
        with self._lock:
            entry = self._entries.get(action_hash)
            if entry is None:
                return False
            if entry.is_expired():
                self._entries.pop(action_hash, None)
                return False
            entry.approved = approved
            return True

    def consume(self, action_hash: str) -> Optional[bool]:
        """Remove and return approval decision, or None if pending/missing."""
        with self._lock:
            entry = self._entries.pop(action_hash, None)
            if entry is None:
                return None
            if entry.is_expired():
                return False    # timeout → denied
            return entry.approved


# Singleton registry used by all approvers
_registry = ApprovalRegistry()


def get_registry() -> ApprovalRegistry:
    return _registry


# ── HUD approver ─────────────────────────────────────────────────────────────

class HudApprover(Approver):
    """
    Approval via the existing PyQt6 HUD confirm banner (core/confirm.py).

    The HUD displays the action summary and two buttons (Approve / Deny).
    This class blocks the calling thread using a threading.Event until
    the user responds or the timeout elapses.
    """

    def __init__(self, confirm_fn=None):
        """
        confirm_fn: callable(message: str, timeout: float) -> bool
          Should be core.confirm.ask_sync or similar.
          When None, a CLI fallback is used (for headless / test contexts).
        """
        self._confirm = confirm_fn or _cli_confirm

    def request(
        self,
        action_summary: str,
        tool: str,
        args: dict,
        timeout: float = 120.0,
    ) -> bool:
        h = _action_hash(tool, args)
        entry = _PendingEntry(action_summary, h, timeout)
        _registry.register(h, entry)

        approved = self._confirm(action_summary, timeout)

        _registry.resolve(h, approved)
        return approved


# ── CLI fallback (headless / test mode) ──────────────────────────────────────

def _cli_confirm(message: str, timeout: float = 120.0) -> bool:
    """
    Simple blocking CLI prompt.  Used when no HUD is available.
    Returns False on timeout, non-interactive stdin, or non-'y' input.
    """
    import sys, os

    # In automated test runners, headless environments, or redirected stdin,
    # prompt cannot be answered interactively — fail closed (deny).
    if not sys.stdin or not hasattr(sys.stdin, "isatty") or not sys.stdin.isatty():
        return False

    prompt = f"\n[!!] APPROVAL REQUIRED ({int(timeout)}s timeout)\n{message}\nApprove? [y/N]: "
    try:
        sys.stdout.write(prompt)
        sys.stdout.flush()
    except UnicodeEncodeError:
        sys.stdout.buffer.write(prompt.encode("ascii", errors="replace") + b"\n")
        sys.stdout.buffer.flush()

    # On Windows select() doesn't work on stdin — use a background thread
    result: list[Optional[bool]] = [None]
    event = threading.Event()

    def _read():
        try:
            ans = input()
            result[0] = ans.strip().lower() == "y"
        except Exception:
            result[0] = False
        event.set()

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    event.wait(timeout=timeout)
    return bool(result[0])   # None -> False (timeout)


# ── Convenience: auto-denial approver for tests ───────────────────────────────

class AutoDenyApprover(Approver):
    """Always returns False. Useful in tests and emergency-paused state."""

    def request(self, action_summary, tool, args, timeout=120.0) -> bool:
        return False


# ── Module-level default approver ─────────────────────────────────────────────

_default_approver: Approver = HudApprover()


def get_approver() -> Approver:
    return _default_approver


def set_approver(approver: Approver) -> None:
    global _default_approver
    _default_approver = approver


def request_approval(
    action_summary: str,
    tool: str,
    args: dict,
    timeout: float = 120.0,
) -> bool:
    """
    Top-level convenience wrapper — call this from action_loader / main.py
    instead of reaching into the approver directly.
    """
    return _default_approver.request(action_summary, tool, args, timeout)
