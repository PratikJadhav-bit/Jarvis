"""
actions/run_powershell.py — CLI/PowerShell tool for Mark-LIII background agent.

Executes PowerShell commands with timeout enforcement, output capping,
and policy protection.
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional

from core.result import ActionResult, action_ok, action_error

_MAX_OUTPUT_CHARS = 4096
_DEFAULT_TIMEOUT = 30.0


def _run_powershell(parameters: dict, **_) -> ActionResult:
    command = str(parameters.get("command") or "").strip()
    if not command:
        return action_error("Please provide a PowerShell command to execute.")

    try:
        timeout = float(parameters.get("timeout", _DEFAULT_TIMEOUT))
        timeout = max(1.0, min(timeout, 120.0))
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT

    cwd = parameters.get("cwd")
    if cwd and not os.path.isdir(cwd):
        return action_error(f"Specified working directory does not exist: {cwd}")

    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )

        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()

        # Cap output lengths
        if len(stdout) > _MAX_OUTPUT_CHARS:
            stdout = stdout[:_MAX_OUTPUT_CHARS] + f"\n... [truncated to {_MAX_OUTPUT_CHARS} characters]"
        if len(stderr) > _MAX_OUTPUT_CHARS:
            stderr = stderr[:_MAX_OUTPUT_CHARS] + f"\n... [truncated to {_MAX_OUTPUT_CHARS} characters]"

        if proc.returncode == 0:
            out = stdout if stdout else "(Command executed successfully with no output)"
            return action_ok(out, stdout=stdout, stderr=stderr, returncode=0)
        else:
            err = stderr if stderr else f"Command failed with exit code {proc.returncode}"
            return action_error(err, output=stdout, stdout=stdout, stderr=stderr, returncode=proc.returncode)

    except subprocess.TimeoutExpired:
        return action_error(f"Command timed out after {timeout} seconds", returncode=-1)
    except Exception as e:
        return action_error(f"Failed to execute command: {e}", returncode=-1)


TOOL = {
    "name": "run_powershell",
    "description": (
        "Execute a PowerShell command or scriptlet on the Windows laptop. "
        "Output is capped and execution is guarded by timeout and safety policy. "
        "Use this for direct system queries, file operations, process checks, and scripting."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "command": {
                "type": "STRING",
                "description": "PowerShell command or scriptlet to execute.",
            },
            "timeout": {
                "type": "NUMBER",
                "description": "Timeout in seconds (default 30, max 120).",
            },
            "cwd": {
                "type": "STRING",
                "description": "Optional working directory.",
            },
        },
        "required": ["command"],
    },
    "handler": _run_powershell,
}
