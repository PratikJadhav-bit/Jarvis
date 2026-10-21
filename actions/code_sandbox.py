"""
Code Sandbox action for JARVIS.
Runs Python/Shell code in isolated Docker containers for safety.
"""

import json
import sys
import subprocess
import tempfile
import os
import shutil
from pathlib import Path
from typing import Optional

def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _get_base_dir()


def _docker_available() -> bool:
    """Check if Docker is available."""
    try:
        result = subprocess.run(["docker", "version"], capture_output=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


def _run_in_docker(code: str, language: str = "python", timeout: int = 30) -> tuple[int, str, str]:
    """Run code in a disposable Docker container."""
    if language == "python":
        image = "python:3.12-slim"
        filename = "script.py"
        cmd = ["python", "/workspace/script.py"]
    elif language == "bash":
        image = "ubuntu:22.04"
        filename = "script.sh"
        cmd = ["bash", "/workspace/script.sh"]
    elif language == "node":
        image = "node:20-slim"
        filename = "script.js"
        cmd = ["node", "/workspace/script.js"]
    else:
        return -1, "", f"Unsupported language: {language}"

    # Create temp directory with code
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        script_path = workspace / filename
        script_path.write_text(code, encoding="utf-8")

        # Make executable if bash
        if language == "bash":
            script_path.chmod(0o755)

        # Run container
        docker_cmd = [
            "docker", "run", "--rm",
            "--network", "none",  # No network access
            "--memory", "512m",   # Memory limit
            "--cpus", "1.0",      # CPU limit
            "--pids-limit", "50", # Process limit
            "--read-only",        # Read-only filesystem
            "--tmpfs", "/workspace:rw,noexec,nosuid,size=100m",  # Writable workspace
            "-v", f"{workspace}:/workspace:ro",  # Mount code as read-only
            "--user", "1000:1000",  # Non-root user
            image,
        ] + cmd

        try:
            result = subprocess.run(docker_cmd, capture_output=True, text=True, timeout=timeout)
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return -1, "", f"Execution timed out after {timeout}s"
        except Exception as e:
            return -1, "", f"Docker error: {e}"


def _run_in_subprocess(code: str, language: str = "python", timeout: int = 30) -> tuple[int, str, str]:
    """Fallback: run in subprocess (less isolated, for systems without Docker)."""
    if language == "python":
        cmd = [sys.executable, "-c", code]
    elif language == "bash":
        cmd = ["bash", "-c", code]
    elif language == "node":
        cmd = ["node", "-e", code]
    else:
        return -1, "", f"Unsupported language: {language}"

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"Execution timed out after {timeout}s"
    except Exception as e:
        return -1, "", f"Execution error: {e}"


def run(parameters: dict, player=None, session_memory=None) -> str:
    code = parameters.get("code", "").strip()
    language = parameters.get("language", "python").lower()
    timeout = int(parameters.get("timeout", 30))
    use_docker = parameters.get("use_docker", True)
    allow_network = parameters.get("allow_network", False)

    if not code:
        return "Provide code to execute."

    # Safety checks
    dangerous_patterns = [
        r"rm\s+-rf\s+/",
        r"mkfs\.",
        r"dd\s+if=.*of=/dev/",
        r":\(\)\{.*:.*\}",  # Fork bomb
        r"chmod\s+777\s+/",
        r"chown\s+-R\s+root",
        r"mount\s+/dev/",
        r"systemctl\s+(stop|disable|mask)",
        r"iptables\s+-F",
    ]
    import re
    for pattern in dangerous_patterns:
        if re.search(pattern, code, re.IGNORECASE):
            return f"Code blocked: dangerous pattern detected ({pattern})"

    if player:
        player.write_log(f"JARVIS: Executing {language} code in {'Docker' if use_docker and _docker_available() else 'subprocess'} sandbox")

    if use_docker and _docker_available():
        returncode, stdout, stderr = _run_in_docker(code, language, timeout)
    else:
        if use_docker and not _docker_available():
            if player:
                player.write_log("JARVIS: Docker not available, falling back to subprocess")
        returncode, stdout, stderr = _run_in_subprocess(code, language, timeout)

    output = ""
    if stdout:
        output += f"STDOUT:\n{stdout}\n"
    if stderr:
        output += f"STDERR:\n{stderr}\n"
    output += f"Exit code: {returncode}"

    # Truncate if too long
    if len(output) > 5000:
        output = output[:5000] + "\n... [truncated]"

    if returncode != 0:
        return f"Code execution failed:\n{output}"

    return output.strip() or "Code executed successfully (no output)."


# ── Tool declaration ──────────────────────────────────────────────────────
TOOL = {
    "name": "code_sandbox",
    "description": "Safely execute Python, Bash, or Node.js code in an isolated Docker container (or subprocess fallback). Use for: running generated code, testing snippets, data analysis, file processing, API calls. Docker mode: no network, read-only FS, memory/CPU limits, non-root user. Subprocess mode: less isolated, works without Docker.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "code": {
                "type": "STRING",
                "description": "Code to execute"
            },
            "language": {
                "type": "STRING",
                "description": "python | bash | node (default: python)"
            },
            "timeout": {
                "type": "INTEGER",
                "description": "Timeout in seconds (default: 30, max: 300)"
            },
            "use_docker": {
                "type": "BOOLEAN",
                "description": "Use Docker sandbox if available (default: true)"
            },
            "allow_network": {
                "type": "BOOLEAN",
                "description": "Allow network access in Docker (default: false, requires explicit opt-in)"
            }
        },
        "required": ["code"]
    },
    "handler": run,
}