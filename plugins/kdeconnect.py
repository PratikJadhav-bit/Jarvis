"""
KDE Connect plugin for JARVIS.
Bridges to Android via KDE Connect (SMS, notifications, clipboard, battery, location, etc.).
Requires: KDE Connect on both PC and phone, paired.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

PLUGIN = {
    "name": "kdeconnect",
    "description": "Control Android phone via KDE Connect. Actions: send_sms, get_sms, get_notifications, get_battery, get_location, ring_phone, share_clipboard, ping. Use for: 'send SMS to Mom', 'read my notifications', 'ring my phone', 'check phone battery'. Requires KDE Connect paired on both devices.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "send_sms | get_sms | get_notifications | get_battery | get_location | ring_phone | share_clipboard | ping | list_devices",
            },
            "device_id": {
                "type": "STRING",
                "description": "KDE Connect device ID (from list_devices). If omitted, uses first paired device.",
            },
            "phone_number": {
                "type": "STRING",
                "description": "Recipient phone number for send_sms",
            },
            "message": {
                "type": "STRING",
                "description": "Message text for send_sms or share_clipboard",
            },
            "limit": {
                "type": "INTEGER",
                "description": "Max items for get_sms/get_notifications (default 10)",
            },
        },
        "required": ["action"],
    },
}

CONFIG_FILE = "config/kdeconnect.json"


def _load_config() -> dict:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_config(cfg: dict) -> None:
    import os
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def _kdeconnect_cli(*args) -> tuple[int, str, str]:
    """Run kdeconnect-cli and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            ["kdeconnect-cli"] + list(args),
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except FileNotFoundError:
        return -1, "", "kdeconnect-cli not found. Install KDE Connect (Windows: Microsoft Store / kdeconnect.kde.org)"
    except subprocess.TimeoutExpired:
        return -1, "", "Command timed out"
    except Exception as e:
        return -1, "", str(e)


def _get_device_id(device_id: Optional[str] = None) -> Optional[str]:
    """Resolve device ID from config or auto-detect first paired device."""
    if device_id:
        return device_id
    cfg = _load_config()
    if cfg.get("device_id"):
        return cfg["device_id"]
    # Auto-detect
    code, out, _ = _kdeconnect_cli("--list-devices")
    if code == 0:
        for line in out.splitlines():
            if "paired" in line.lower() or "connected" in line.lower():
                # Parse: "DeviceName (device_id) - status"
                import re
                match = re.search(r'\(([a-f0-9-]+)\)', line)
                if match:
                    return match.group(1)
    return None


def test_connection(device_id: str = "") -> str:
    """Test KDE Connect connection."""
    did = _get_device_id(device_id) if device_id else _get_device_id()
    if not did:
        return "No paired KDE Connect device found. Pair your phone first."
    code, out, err = _kdeconnect_cli("--ping-msg", "test", "--device", did)
    if code == 0:
        return f"KDE Connect OK — device {did} responded."
    return f"Failed: {err or out}"


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = parameters.get("action", "").lower().strip()
    device_id = parameters.get("device_id", "").strip() or None
    phone_number = parameters.get("phone_number", "").strip()
    message = parameters.get("message", "").strip()
    limit = int(parameters.get("limit", 10))

    did = _get_device_id(device_id)
    if not did and action != "list_devices":
        return "No KDE Connect device. Run list_devices or pair your phone."

    try:
        if action == "list_devices":
            code, out, err = _kdeconnect_cli("--list-devices")
            if code != 0:
                return f"Failed: {err}"
            return out or "No devices found."

        if action == "ping":
            code, out, err = _kdeconnect_cli("--ping-msg", "JARVIS ping", "--device", did)
            return "Ping sent." if code == 0 else f"Failed: {err}"

        if action == "ring_phone":
            code, out, err = _kdeconnect_cli("--ring", "--device", did)
            return "Phone ringing." if code == 0 else f"Failed: {err}"

        if action == "send_sms":
            if not phone_number or not message:
                return "send_sms requires phone_number and message."
            code, out, err = _kdeconnect_cli("--send-sms", message, "--destination", phone_number, "--device", did)
            return "SMS sent." if code == 0 else f"Failed: {err}"

        if action == "get_sms":
            code, out, err = _kdeconnect_cli("--get-sms", "--device", did)
            if code != 0:
                return f"Failed: {err}"
            lines = out.splitlines()[:limit]
            return "Recent SMS:\n" + "\n".join(lines) if lines else "No SMS found."

        if action == "get_notifications":
            code, out, err = _kdeconnect_cli("--get-notifications", "--device", did)
            if code != 0:
                return f"Failed: {err}"
            lines = out.splitlines()[:limit]
            return "Notifications:\n" + "\n".join(lines) if lines else "No notifications."

        if action == "get_battery":
            code, out, err = _kdeconnect_cli("--get-battery", "--device", did)
            return out if code == 0 else f"Failed: {err}"

        if action == "get_location":
            code, out, err = _kdeconnect_cli("--get-location", "--device", did)
            return out if code == 0 else f"Failed: {err}"

        if action == "share_clipboard":
            if not message:
                return "Provide message to send to phone clipboard."
            code, out, err = _kdeconnect_cli("--share-text", message, "--device", did)
            return "Clipboard shared." if code == 0 else f"Failed: {err}"

        return f"Unknown action: {action}. Use: list_devices, ping, ring_phone, send_sms, get_sms, get_notifications, get_battery, get_location, share_clipboard."

    except Exception as e:
        return f"KDE Connect error: {e}"


PLUGIN_SETTINGS = {
    "namespace": "kdeconnect",
    "title": "KDE Connect (Android)",
    "fields": [
        {"key": "device_id", "label": "Device ID (optional)", "type": "text", "placeholder": "auto-detect from paired devices"},
    ],
    "action": {"label": "Test Connection", "run": test_connection},
}