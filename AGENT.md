# Mark-LIII Autonomous Agent Architecture & Operator Guide

Mark-LIII is an autonomous, voice-driven personal agent designed exclusively for Windows laptops. It extends the core Mark-LIII voice frontend, wake word listener, and PyQt6 HUD with a background agent execution runtime, safety policy enforcement, self-correction loop, adaptive task learning, and secure remote control via Telegram.

---

## 1. System Architecture

```text
+-----------------------------------------------------------------------------------+
|                                  USER INTERFACES                                  |
|                                                                                   |
|  [Voice / Mic]  --> (Wake Word: "Jarvis") --> [Gemini Live Voice Client]          |
|  [PyQt6 HUD]    <-- (Speech Queue / Toasts)  <-- [Notifier Subsystem]             |
|  [System Tray]  --> (Minimize to tray, Run in background, Emergency Stop)        |
|  [Telegram Bot] <-> (1:1 Whitelisted Chat, Commands, Inline Approvals, Voice Notes)|
+-----------------------------------------+-----------------------------------------+
                                          |
                                 Submits Task / Command
                                          v
+-----------------------------------------------------------------------------------+
|                         BACKGROUND TASK ENGINE (core/)                           |
|                                                                                   |
|   +-----------------------+              +------------------------------------+   |
|   |   Task Queue          | <--- poll -- |  JarvisTaskWorker (Daemon Thread)  |   |
|   |   (data/tasks.db)     |              |  - Priority Queue (HIGH/NORMAL/LOW)|   |
|   |   - SQLite persistent |              |  - Stale Task Auto-Recovery        |   |
|   |   - State Transitions |              |  - Emergency Stop / Pause Check    |   |
|   +-----------------------+              +-----------------+------------------+   |
+------------------------------------------------------------|----------------------+
                                                             | Launches
                                                             v
+-----------------------------------------------------------------------------------+
|                        AUTONOMOUS AGENT LOOP (core/agent_loop.py)                 |
|                                                                                   |
|   1. ACQUIRE EXECUTION CONTEXT                                                    |
|      - KeepAwake (ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)                       |
|      - Check ScreenLock (Pause GUI actions if Win32 Desktop is locked)            |
|                                                                                   |
|   2. RECIPE LOOKUP & REPLAY                                                       |
|      - Query LearningDB (memory/learning.db FTS5) for verified recipe            |
|      - Fast-path replay if verified and input matching                            |
|                                                                                   |
|   3. OBSERVE -> DECIDE -> ACT -> VERIFY LOOP                                      |
|      - LLM Step Planning (Structured Output, Strict Tool Preference)              |
|      - Safety Policy Gate (core/policy.py: AUTO / NOTIFY / CONFIRM / DENY)       |
|      - Interactive Approvals (HUD Prompt or Telegram Inline Button)               |
|      - Append-Only Audit Trail (logs/audit.db with Secret Redaction)             |
|      - Execution (actions/*.py auto-discovered via TOOL dicts)                   |
|      - Step Output Verification & Self-Correction (Max 15 steps / Time Budget)    |
|                                                                                   |
|   4. RUN DISTILLATION & LEARNING                                                  |
|      - Extract durable preferences to core/memory_manager                         |
|      - Index successful execution sequence into LearningDB                        |
|      - Release KeepAwake context                                                  |
+-----------------------------------------------------------------------------------+
```

---

## 2. Starting & Stopping the Agent

### Prerequisites
- Windows 10 or 11 (64-bit)
- Python 3.10+ (in `.venv`)
- Windows PowerShell 5.1+ or PowerShell 7+

### Running Mark-LIII
Activate the virtual environment and start the primary UI application:
```powershell
& "..\.venv\Scripts\Activate.ps1"
python main.py
```

### Running in Background Mode
- When closed, Mark-LIII automatically minimizes to the Windows System Notification Tray.
- Right-click the system tray icon to view active background tasks, trigger an **Emergency Stop**, or exit completely.

### Emergency Stop (Kill Switch)
If the agent is performing an unintended operation:
1. **Global Keyboard Shortcut:** Press `Ctrl + Alt + Shift + K` from any application.
2. **Spoken Voice Command:** Say `"emergency stop"`, `"kill switch"`, or `"jarvis stop everything"`.
3. **Telegram Command:** Send `/stop` to your configured Telegram bot.
4. **System Tray:** Click **Emergency Stop** in the tray menu.

All running background tasks will immediately terminate, pending tasks will be marked cancelled, and further execution will remain paused until explicitly resumed via `/resume` or `main.py`.

---

## 3. Safety Policy & Action Tiers

Mark-LIII categorizes every tool and command into four distinct safety tiers defined in `config/policy.json`.

| Tier | Policy Behavior | Examples |
| :--- | :--- | :--- |
| **AUTO** | Allowed unconditionally in background and unattended mode. | Reading files, searching web, checking weather, reminders, `git status`. |
| **NOTIFY** | Executes automatically, but immediately pushes a toast notification and audit log. | Opening applications, file modifications in project directories. |
| **CONFIRM** | Requires explicit user authorization before execution. Prompts HUD or Telegram with cryptographic hash token. Expired after 60s. | Overwriting unknown files, running non-destructive PowerShell commands, `git push`. |
| **DENY** | Permanently blocked under all circumstances. Never executed. | Format drive, delete `C:\Windows`, registry purge, credential harvesting. |

### Editing Policy Rules
Edit `config/policy.json` to alter permission tiers:
```json
{
  "tools": {
    "file_processor": "AUTO",
    "run_powershell": "CONFIRM",
    "ui_automation": "CONFIRM"
  },
  "deny_commands": [
    "format ",
    "rmdir /s /q c:",
    "Remove-Item -Recurse -Force C:\\Windows"
  ]
}
```
*Note: Any unlisted action dynamically defaults to `CONFIRM` for safety.*

---

## 4. Unattended Mode

When working away from your laptop, you can place Mark-LIII into **Unattended Mode**:
- **Activate via Voice:** *"Jarvis, set unattended mode for 2 hours."*
- **Activate via Telegram:** Send `/unattended 120` (in minutes).
- **Behavior:**
  - Tasks in `AUTO` and `NOTIFY` tiers execute uninterrupted.
  - High-risk `CONFIRM` actions do not stall waiting for desktop HUD input; instead, they route directly to your Telegram bot as an interactive approval message with `[Approve]` and `[Deny]` buttons.
  - Auto-reverts to standard attended mode once the specified duration elapses.

---

## 5. Remote Telegram Control Setup

Mark-LIII features a secure, long-polling Telegram channel that allows remote task submission, monitoring, and interactive confirmation.

### Step-by-Step Setup:
1. **Create a Bot via BotFather:**
   - In Telegram, open a chat with `@BotFather`.
   - Send `/newbot`, choose a name and a username ending in `bot`.
   - Copy the generated API Token (e.g., `123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ`).

2. **Save Token Securely:**
   Mark-LIII retrieves secrets from Windows Credential Manager or environment variables:
   ```powershell
   # Option A: Store in Windows Credential Manager (Target: Jarvis_Telegram_Token)
   # Option B: Set Environment Variable
   [System.Environment]::SetEnvironmentVariable('TELEGRAM_BOT_TOKEN', 'your-bot-token-here', 'User')
   ```

3. **Configure Allowed User ID Whitelist:**
   - In Telegram, chat with `@userinfobot` to retrieve your numeric Telegram User ID (e.g., `987654321`).
   - Add this ID to `config/telegram.json` (the single real Telegram configuration file):
     ```json
     {
       "allowed_user_ids": [987654321],
       "enabled": true
     }
     ```
   - *(Note: `core/telegram_channel.py` also accepts `config/config.json` with a `"telegram"` block for backwards compatibility).*
   - *Security Note: Any messages from unauthorized IDs or group chats are silently dropped with zero response.*

### Available Telegram Commands:
- `/status` — View current agent status, queue depth, and active task info.
- `/tasks` — List pending and recent tasks with their states.
- `/cancel <task_id>` — Cancel a specific background task.
- `/stop` — Immediate Emergency Stop (halts and pauses all operations).
- `/resume` — Unpause the agent after an emergency stop.
- `/screen` — Capture an encrypted screenshot of the primary desktop and send as photo.
- `/unattended <minutes>` — Engage unattended mode with approval routing.
- **Voice Notes:** Record and send a voice memo in Telegram. Mark-LIII will transcribe it and submit it as a background task.

---

## 6. Adaptive Learning & Self-Improvement

Mark-LIII automatically learns from past command executions to speed up repeated tasks without repetitive LLM re-planning.

### How Learning Works:
1. **Run Distillation:** When an autonomous task succeeds, `core/learning.py` records the goal, action sequence, parameter templates, and verification criteria into `memory/learning.db` (backed by SQLite FTS5).
2. **Verified Replay:** On subsequent tasks, Mark-LIII checks if a matching recipe exists:
   - If verified, it executes the pre-compiled step sequence directly (Run 2 operates in <0.2s with zero LLM API calls).
   - If a step in a replayed recipe fails, the replay instantly aborts, clears state, and falls back to dynamic LLM problem solving.
3. **Preference Extraction:** Personal user preferences (e.g., favorite directories, output formats) are extracted and persisted to `memory/` using the core `MemoryManager`.

### Managing Learned Recipes:
- **List Learned Skills:** Spoken command: *"Jarvis, list learned skills"* or action `list_learned`.
- **Forget a Learned Skill:** Spoken command: *"Jarvis, forget how to deploy project"* or action `forget_learned(query="deploy project")`.
- **Database Inspection:** Stored locally in `memory/learning.db`.

---

## 7. Known Limits & Hardware Constraints

While Mark-LIII is designed to operate autonomously while you are away, the following constraints apply:

1. **Locked Screen GUI Limitations:**
   - When Windows is locked (`Win + L`) or the screensaver engages, Windows switches to the secure logon desktop.
   - Operating system isolation prevents synthetic mouse clicks and keyboard automation (`pywinauto`, `universal_control`) from interacting with locked desktop windows.
   - Mark-LIII detects screen lock state via `core/screen_lock.py` and gracefully pauses GUI tasks as `BLOCKED`. Non-GUI actions (PowerShell scripts, file operations, web queries, API requests) continue running normally.
2. **Windows UAC & Secure Desktop:**
   - Elevation prompts (User Account Control) run on an isolated secure desktop. The agent cannot click "Yes" on an unprompted administrative UAC elevation dialog.
3. **Hardware Sleep & Power Settings:**
   - `core/keep_awake.py` asserts Windows execution flags (`ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED`) while a task is running.
   - If the laptop lid is closed and Windows power policy is configured to force sleep on lid close, execution may suspend. For full unattended capability, set Windows power plan to *"Do nothing"* on lid close when plugged into AC power.
4. **Network & LLM Connectivity:**
   - Active internet connectivity is required for Gemini Live voice processing, cloud LLM steps, and Telegram long-polling. Local actions (PowerShell, file operations, cached recipe replay) will function offline if a local LLM fallback is configured.
5. **Web CAPTCHAs & Anti-Bot Shields:**
   - Autonomous web navigation will not solve interactive Cloudflare Turnstile or CAPTCHA challenges without manual intervention.

---

## 8. Verified on Real OS Status

Below is the honest, auditable record of what was directly executed and verified on the actual Windows host OS versus simulated/unit-tested:

| Feature / Subsystem | Tested on Real OS? | Exact Verification Command | Actual Result / Caveat |
| :--- | :--- | :--- | :--- |
| **Win32 DPI Awareness** | **Yes** | `python -c "import ctypes; print(bool(ctypes.windll.user32.SetProcessDPIAware()))"` | Exited `0`, returned `True`. Windows process DPI awareness registered successfully. |
| **Win32 Keep-Awake (`SetThreadExecutionState`)** | **Yes** | `python -c "import ctypes; print(hex(ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)))"` | Exited `0`, successfully called Win32 API and received valid previous state mask. |
| **Win32 Screen Lock Detection** | **Partial (Unlocked Only)** | `python -c "from core.screen_lock import is_screen_locked; print(is_screen_locked())"` | Returned `False` on the active desktop. **Plain statement: The actual locked desktop case (`Win + L`) was NOT tested on the real OS**, as locking the workstation terminates interactive agent execution. |
| **SQLite FTS5 Full-Text Search** | **Yes** | `python -c "import sqlite3; con = sqlite3.connect(':memory:'); con.execute('CREATE VIRTUAL TABLE t USING fts5(x)'); print('FTS5 OK')"` | Exited `0`, FTS5 extension is natively compiled and active in Python on this host. |
| **PowerShell Execution (`run_powershell`)** | **Yes** | `python -m unittest tests.test_phase3.TestPowershellTool` | Exited `0` across 6 tests. Real Windows `powershell.exe` process spawned, executed commands, capped output, and enforced timeouts. |
| **PyQt6 System Tray & HUD Lifecycle** | **Partial** | `python -m unittest tests.test_phase2.TestSystemTrayMode` | PyQt6 modules load and configure system tray menus in test harness. Interactive desktop rendering requires starting `main.py`. |
| **Telegram Remote Channel** | **Simulated Transport** | `python -m unittest tests.test_phase5` | Core long-polling logic, message parsing, command handling, and inline button token verification tested with `MockTelegramTransport`. Real HTTP polling requires live Telegram bot token. |

