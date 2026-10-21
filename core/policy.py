"""
core/policy.py — Safety policy engine for Mark-LIII.

Every tool call is classified into one of four tiers before execution:

  AUTO    — read-only / harmless: run silently.
  NOTIFY  — reversible change: run, log, tell the user.
  CONFIRM — irreversible or risky: wait for explicit human approval.
  DENY    — always blocked; approval cannot unlock.

The tier map lives in config/policy.json (user-editable). Unknown tools
default to CONFIRM. The PowerShell / shell command classifier checks the
command text against allow/deny patterns before policy.json lookup.

Taint tracking: a task is flagged ``tainted`` when it ingests untrusted
content (web page, file contents, email, screen OCR). In a tainted context,
NOTIFY-tier actions that send data outward or run shell commands are
escalated to CONFIRM automatically.

Protected paths: core/policy.py, config/policy.json,
config/unattended_allowlist.json, core/approvals.py — any write targeting
these paths is DENY regardless of tier map.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

# ── Tier ────────────────────────────────────────────────────────────────────

class Tier(str, Enum):
    AUTO    = "AUTO"
    NOTIFY  = "NOTIFY"
    CONFIRM = "CONFIRM"
    DENY    = "DENY"


@dataclass
class Decision:
    tier:   Tier
    reason: str = ""


# ── Protected paths (writes are always DENY) ────────────────────────────────

_BASE = Path(__file__).resolve().parent.parent

PROTECTED_PATHS: set[str] = {
    str(_BASE / "core" / "policy.py"),
    str(_BASE / "core" / "approvals.py"),
    str(_BASE / "config" / "policy.json"),
    str(_BASE / "config" / "unattended_allowlist.json"),
}

def _is_protected_write(tool: str, args: dict) -> bool:
    """Return True if this call tries to write to a protected file."""
    write_tools = {
        "file_controller", "write_file", "save_file",
        "run_powershell", "run_shell", "code_sandbox",
    }
    if tool not in write_tools:
        return False
    # Check any string arg for a protected path substring
    candidates = list(args.values()) if isinstance(args, dict) else []
    for v in candidates:
        if isinstance(v, str):
            for pp in PROTECTED_PATHS:
                if pp.lower() in v.replace("\\", "/").lower() or \
                   Path(pp).name.lower() in v.lower():
                    return True
    return False


# ── PowerShell / shell classifier ───────────────────────────────────────────

# Patterns whose presence makes a command DENY (regardless of anything else).
_DENY_CMD_PATTERNS: list[re.Pattern] = [p for p in (
    re.compile(r"\bformat\b",            re.I),
    re.compile(r"\bdiskpart\b",          re.I),
    re.compile(r"\bcipher\s+/w\b",       re.I),
    re.compile(r"reg\s+delete.*HKLM",    re.I),
    re.compile(r"\bnet\s+user\b",        re.I),
    re.compile(r"Set-ExecutionPolicy",   re.I),
    re.compile(r"\bvssadmin\b",          re.I),
    re.compile(r"Disable-(Windows)?Defender|Set-MpPreference.*Disable", re.I),
    # curl|wget piped into interpreter
    re.compile(r"(curl|wget|iwr).*\|\s*(iex|Invoke-Expression|bash|sh|python|pwsh)", re.I),
    re.compile(r"IEX\s*\(", re.I),
)]

# Patterns that bump a command to CONFIRM.
_CONFIRM_CMD_PATTERNS: list[re.Pattern] = [p for p in (
    re.compile(r"Remove-Item.*-Recurse",  re.I),
    re.compile(r"\brm\s+-rf\b",           re.I),
    re.compile(r"\bdel\s+/[fqs]",         re.I),
    re.compile(r"Stop-Service|sc\s+stop", re.I),
    re.compile(r"New-LocalUser|Add-LocalGroupMember", re.I),
    re.compile(r"schtasks.*/(create|delete|run|change)", re.I),
    re.compile(r"netsh\s+(firewall|advfirewall|wlan)", re.I),
    re.compile(r"\bshutdown\b",           re.I),
    re.compile(r"\brestart-computer\b",   re.I),
    re.compile(r"Invoke-WebRequest.*-OutFile", re.I),
)]


def _classify_shell_command(cmd: str) -> Optional[Tier]:
    """Return the tier implied by shell command content, or None to fall through to policy.json."""
    if not cmd or not isinstance(cmd, str):
        return None
    for pat in _DENY_CMD_PATTERNS:
        if pat.search(cmd):
            return Tier.DENY
    for pat in _CONFIRM_CMD_PATTERNS:
        if pat.search(cmd):
            return Tier.CONFIRM
    return None   # caller falls through to policy.json default


# ── Policy map (loaded from config/policy.json) ─────────────────────────────

_POLICY_PATH = _BASE / "config" / "policy.json"
_policy_cache: dict | None = None
_policy_lock = threading.Lock()


def _load_policy() -> dict:
    global _policy_cache
    with _policy_lock:
        if _policy_cache is not None:
            return _policy_cache
        try:
            _policy_cache = json.loads(_POLICY_PATH.read_text(encoding="utf-8"))
        except Exception:
            _policy_cache = {}
        return _policy_cache


def reload_policy() -> None:
    """Force reload policy.json on next check() call."""
    global _policy_cache
    with _policy_lock:
        _policy_cache = None


def _tier_from_policy(tool: str) -> Tier:
    """Look up tool in policy.json; default CONFIRM for unknowns."""
    policy = _load_policy()
    tiers: dict = policy.get("tiers", {})
    raw = tiers.get(tool)
    if raw is None:
        return Tier.CONFIRM   # unknown → safest
    try:
        return Tier(raw.upper())
    except ValueError:
        return Tier.CONFIRM


# ── Taint escalation helpers ─────────────────────────────────────────────────

# Tools that send data outward (only escalated when tainted)
_OUTWARD_TOOLS = {
    "send_message", "browser_control", "web_search",
    "run_powershell", "run_shell", "code_sandbox",
    "file_controller",   # could upload
}


def _maybe_escalate_for_taint(tier: Tier, tool: str, tainted: bool) -> tuple[Tier, str]:
    """Escalate NOTIFY → CONFIRM for outward tools in a tainted context."""
    if not tainted:
        return tier, ""
    if tier == Tier.NOTIFY and tool in _OUTWARD_TOOLS:
        return Tier.CONFIRM, "escalated: tainted context + outward action"
    return tier, ""


# ── Unattended mode ──────────────────────────────────────────────────────────

_UNATTENDED_ALLOWLIST_PATH = _BASE / "config" / "unattended_allowlist.json"
_unattended: bool = False
_unattended_lock = threading.Lock()


def is_unattended() -> bool:
    with _unattended_lock:
        return _unattended


def set_unattended(on: bool) -> None:
    global _unattended
    with _unattended_lock:
        _unattended = bool(on)


def _allowlisted_unattended(tool: str, args: dict) -> bool:
    """Return True if tool+args match the unattended allowlist."""
    try:
        allowlist = json.loads(_UNATTENDED_ALLOWLIST_PATH.read_text(encoding="utf-8"))
        patterns: list[str] = allowlist.get("allowed_tools", [])
        return any(re.fullmatch(p, tool, re.I) for p in patterns)
    except Exception:
        return False   # no list → deny unattended


# ── Kill-switch state ────────────────────────────────────────────────────────

_paused = False
_paused_lock = threading.Lock()


def is_paused() -> bool:
    with _paused_lock:
        return _paused


def set_paused(on: bool) -> None:
    global _paused
    with _paused_lock:
        _paused = bool(on)


# ── Main API ─────────────────────────────────────────────────────────────────

def check(tool: str, args: dict | None = None, context: dict | None = None) -> Decision:
    """
    Classify a tool call and return a Decision(tier, reason).

    context kwargs honoured:
      tainted (bool)  — task has ingested untrusted content
      source  (str)   — 'voice' | 'remote' | 'macro'

    Rules applied in order:
      1. Kill-switch PAUSED  → DENY
      2. Protected path write → DENY
      3. Shell command classifier (run_powershell / code_sandbox)
      4. policy.json tier map
      5. Taint escalation
      6. Unattended mode (non-allowlisted → CONFIRM)
    """
    args    = args or {}
    context = context or {}
    tainted = bool(context.get("tainted", False))

    # 1. System paused
    if is_paused():
        return Decision(Tier.DENY, "system paused — kill switch active")

    # 2. Protected path writes
    if _is_protected_write(tool, args):
        return Decision(Tier.DENY, f"write to protected safety file blocked")

    # 3. Shell command classifier
    if tool in ("run_powershell", "run_shell", "code_sandbox"):
        cmd = args.get("command") or args.get("code") or args.get("script") or ""
        shell_tier = _classify_shell_command(cmd)
        if shell_tier == Tier.DENY:
            return Decision(Tier.DENY, f"shell command matched DENY pattern: {cmd[:80]!r}")
        if shell_tier == Tier.CONFIRM:
            reason = f"shell command requires approval: {cmd[:80]!r}"
            tier, extra = _maybe_escalate_for_taint(Tier.CONFIRM, tool, tainted)
            return Decision(Tier.CONFIRM, reason + (f"; {extra}" if extra else ""))
        # else None → fall through to policy.json

    # 4. policy.json tier
    tier = _tier_from_policy(tool)
    reason = f"policy tier {tier.value} for tool {tool!r}"

    # 5. Taint escalation
    tier, extra = _maybe_escalate_for_taint(tier, tool, tainted)
    if extra:
        reason += f"; {extra}"

    # 6. Unattended mode
    if is_unattended() and tier in (Tier.NOTIFY, Tier.CONFIRM):
        if not _allowlisted_unattended(tool, args):
            return Decision(Tier.CONFIRM, f"unattended mode: {tool!r} requires approval")

    return Decision(tier, reason)
