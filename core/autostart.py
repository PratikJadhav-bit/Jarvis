"""
core/autostart.py — Windows logon auto-start registration for Mark-LIII.

Provides logon startup registration via:
  1. Windows Task Scheduler (`schtasks.exe` logon task named JARVIS_Logon)
  2. Windows Registry Run Key (`HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run`)
"""
from __future__ import annotations

import logging
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

_log = logging.getLogger("autostart")
TASK_NAME = "JARVIS_Agent_Logon"
REG_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
REG_VALUE_NAME = "JARVIS_AI"


def is_windows() -> bool:
    return platform.system() == "Windows"


def get_startup_command() -> tuple[str, str]:
    """Return (executable_path, arguments_str) for background logon."""
    base_dir = Path(__file__).resolve().parent.parent
    script = str(base_dir / "main.py")
    pythonw = Path(sys.executable).parent / "pythonw.exe"
    exe = str(pythonw if pythonw.exists() else sys.executable)
    args = f'"{script}" --background'
    return exe, args


def register_task_scheduler_logon(task_name: str = TASK_NAME) -> bool:
    """
    Register logon task via Task Scheduler (schtasks.exe).
    Returns True if scheduled task was created successfully.
    """
    if not is_windows():
        return False

    exe, args = get_startup_command()
    xml_content = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{exe}</Command>
      <Arguments>{args}</Arguments>
    </Exec>
  </Actions>
</Task>"""

    tmp_xml = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".xml", delete=False, mode="w", encoding="utf-16") as f:
            f.write(xml_content)
            tmp_xml = f.name

        cmd = ["schtasks", "/Create", "/TN", task_name, "/XML", tmp_xml, "/F"]
        res = subprocess.run(cmd, capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if res.returncode == 0:
            _log.info(f"Task Scheduler logon task '{task_name}' registered successfully.")
            return True
        else:
            _log.warning(f"Task Scheduler registration returned {res.returncode}: {res.stderr or res.stdout}")
            return False
    except Exception as e:
        _log.warning(f"Failed to register Task Scheduler logon: {e}")
        return False
    finally:
        if tmp_xml and os.path.exists(tmp_xml):
            try:
                os.unlink(tmp_xml)
            except Exception:
                pass


def unregister_task_scheduler_logon(task_name: str = TASK_NAME) -> bool:
    """Remove logon task from Task Scheduler."""
    if not is_windows():
        return False
    try:
        cmd = ["schtasks", "/Delete", "/TN", task_name, "/F"]
        res = subprocess.run(cmd, capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return res.returncode == 0
    except Exception:
        return False


def register_registry_run_key() -> bool:
    """Register startup command in HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run."""
    if not is_windows():
        return False
    try:
        import winreg
        exe, args = get_startup_command()
        cmd_str = f'"{exe}" {args}'
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_RUN_KEY, 0, winreg.KEY_ALL_ACCESS) as key:
            winreg.SetValueEx(key, REG_VALUE_NAME, 0, winreg.REG_SZ, cmd_str)
        return True
    except Exception as e:
        _log.warning(f"Failed to set HKCU Run key: {e}")
        return False


def unregister_registry_run_key() -> bool:
    """Delete startup command from HKCU Run key."""
    if not is_windows():
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_RUN_KEY, 0, winreg.KEY_ALL_ACCESS) as key:
            try:
                winreg.DeleteValue(key, REG_VALUE_NAME)
                return True
            except FileNotFoundError:
                return True
    except Exception:
        return False


def register_logon_startup() -> bool:
    """
    Register logon auto-start using Task Scheduler with Registry Run key fallback.
    Returns True if at least one startup mechanism was successfully configured.
    """
    ts_ok = register_task_scheduler_logon()
    reg_ok = register_registry_run_key()
    return ts_ok or reg_ok


def unregister_logon_startup() -> bool:
    """Unregister logon auto-start across Task Scheduler and Registry."""
    ts_ok = unregister_task_scheduler_logon()
    reg_ok = unregister_registry_run_key()
    return ts_ok or reg_ok
