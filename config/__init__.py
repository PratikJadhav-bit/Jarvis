# config/__init__.py
"""
Central configuration module for Mark-LIII.
Provides centralized model names, OS detection, secure secret management
(Environment variables, Windows Credential Manager / keyring, and fallback),
and repository safety (.gitignore verification).
"""
import json
import os
import platform
import subprocess
from pathlib import Path
from typing import Optional

_CONFIG_PATH = Path(__file__).parent / "api_keys.json"
BASE_DIR = Path(__file__).resolve().parent.parent

# ── Centralized Model Names ────────────────────────────────────────────────
DEFAULT_MODEL = "gemini-flash-latest"
DEFAULT_LIVE_MODEL = "models/gemini-3.1-flash-live-preview"
DEFAULT_PLANNER_MODEL = "gemini-flash-latest"
DEFAULT_VISION_MODEL = "gemini-flash-latest"

MODEL_REGISTRY: dict[str, str] = {
    "default": DEFAULT_MODEL,
    "planner": DEFAULT_PLANNER_MODEL,
    "universal_control": DEFAULT_VISION_MODEL,
    "vision": DEFAULT_VISION_MODEL,
    "live": DEFAULT_LIVE_MODEL,
    "web_search": DEFAULT_MODEL,
    "dev_agent": DEFAULT_MODEL,
    "file_processor": DEFAULT_MODEL,
}


def get_model(key: str = "default") -> str:
    """
    Get configured model name for a specific component.
    Hierarchy:
    1. Environment variable (e.g. GEMINI_MODEL or GEMINI_MODEL_<KEY>)
    2. config/api_keys.json (key 'gemini_model' or 'models' dict)
    3. MODEL_REGISTRY default
    """
    env_specific = os.environ.get(f"GEMINI_MODEL_{key.upper()}")
    if env_specific:
        return env_specific

    env_general = os.environ.get("GEMINI_MODEL")
    if env_general:
        return env_general

    cfg = get_config()
    models_cfg = cfg.get("models", {})
    if isinstance(models_cfg, dict) and key in models_cfg:
        return str(models_cfg[key])

    if "gemini_model" in cfg and cfg["gemini_model"]:
        return str(cfg["gemini_model"])

    return MODEL_REGISTRY.get(key, DEFAULT_MODEL)


def get_live_model() -> str:
    """Get model name for the Gemini Live session."""
    return os.environ.get("GEMINI_LIVE_MODEL") or get_config().get("live_model", DEFAULT_LIVE_MODEL)


# ── OS Detection ───────────────────────────────────────────────────────────
def _platform_os() -> str:
    """Auto-detect OS when config file is absent."""
    return {"Windows": "windows", "Darwin": "mac", "Linux": "linux"}.get(
        platform.system(), "linux"
    )


def get_config() -> dict:
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def get_os() -> str:
    """Returns: 'windows' | 'mac' | 'linux'"""
    return get_config().get("os_system", _platform_os()).lower()


def is_windows() -> bool:
    return get_os() == "windows"


def is_mac() -> bool:
    return get_os() == "mac"


def is_linux() -> bool:
    return get_os() == "linux"


# ── Secret Management (Env -> Windows Credential Manager / keyring -> file) ─
SERVICE_NAME = "Mark-LIII"


def _read_win32cred(target_name: str) -> Optional[str]:
    """Read credential from Windows Credential Manager via win32cred or ctypes."""
    try:
        import win32cred
        cred = win32cred.CredRead(target_name, win32cred.CRED_TYPE_GENERIC, 0)
        blob = cred.get("CredentialBlob", b"")
        if isinstance(blob, bytes):
            return blob.decode("utf-16", errors="replace")
        return str(blob)
    except Exception:
        pass

    try:
        import keyring
        val = keyring.get_password(SERVICE_NAME, target_name)
        if val:
            return val
    except Exception:
        pass

    return None


def _write_win32cred(target_name: str, secret: str) -> bool:
    """Write credential to Windows Credential Manager via win32cred or keyring."""
    try:
        import win32cred
        cred = {
            "Type": win32cred.CRED_TYPE_GENERIC,
            "TargetName": target_name,
            "CredentialBlob": secret.encode("utf-16"),
            "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
        }
        win32cred.CredWrite(cred, 0)
        return True
    except Exception:
        pass

    try:
        import keyring
        keyring.set_password(SERVICE_NAME, target_name, secret)
        return True
    except Exception:
        pass

    return False


def get_api_key(name: str = "gemini_api_key") -> str:
    """
    Retrieve secret with precedence:
    1. Environment variable (GEMINI_API_KEY, GOOGLE_API_KEY, or exact name uppercase)
    2. Windows Credential Manager (keyring / win32cred)
    3. config/api_keys.json (legacy fallback)
    """
    # 1. Environment variables
    if name == "gemini_api_key":
        for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "JARVIS_API_KEY"):
            val = os.environ.get(var)
            if val and len(val.strip()) > 5:
                return val.strip()
    env_val = os.environ.get(name.upper())
    if env_val and len(env_val.strip()) > 5:
        return env_val.strip()

    # 2. Windows Credential Manager / keyring
    target = f"{SERVICE_NAME}:{name}"
    cred_val = _read_win32cred(target)
    if cred_val and len(cred_val.strip()) > 5:
        return cred_val.strip()

    # 3. Legacy api_keys.json
    cfg = get_config()
    file_val = cfg.get(name, "")
    if file_val and len(file_val.strip()) > 5:
        return file_val.strip()

    return ""


def set_api_key(key: str, name: str = "gemini_api_key", store_in_keyring: bool = True) -> bool:
    """
    Save secret to Windows Credential Manager / keyring, or fallback to file.
    """
    key = key.strip()
    saved = False
    if store_in_keyring:
        target = f"{SERVICE_NAME}:{name}"
        saved = _write_win32cred(target, key)

    if not saved:
        # Fallback to config file
        try:
            cfg = get_config()
            cfg[name] = key
            with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
            saved = True
        except Exception:
            saved = False

    return saved


# ── .gitignore Verification ────────────────────────────────────────────────
def check_gitignore(repo_path: Optional[Path] = None) -> tuple[bool, list[str]]:
    """
    Check if sensitive files are properly ignored by .gitignore.
    Returns: (is_safe: bool, issues: list[str])
    """
    root = repo_path or BASE_DIR
    gitignore_file = root / ".gitignore"
    issues = []

    if not gitignore_file.exists():
        return False, ["Missing .gitignore file in repository root."]

    content = gitignore_file.read_text(encoding="utf-8", errors="replace")
    lines = [line.strip() for line in content.splitlines()]

    critical_patterns = [
        "config/api_keys.json",
        ".env",
    ]

    for pat in critical_patterns:
        # Check if the exact pattern is on its own line (not with trailing inline comments)
        matched = any(line == pat or line.startswith(f"{pat} ") for line in lines)
        if not matched:
            issues.append(f"Pattern '{pat}' not cleanly listed on its own line in .gitignore.")

    # Also check git check-ignore if inside a git repo
    test_files = [
        "config/api_keys.json",
        ".env",
        "token_test.json",
        "client_secret_test.json"
    ]
    for tf in test_files:
        try:
            res = subprocess.run(
                ["git", "check-ignore", "-q", tf],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=5
            )
            if res.returncode != 0:
                issues.append(f"Git does not ignore '{tf}' according to git check-ignore.")
        except Exception:
            # git may not be installed or not a git repo
            pass

    return (len(issues) == 0, issues)
