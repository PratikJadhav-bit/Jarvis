"""
Universal Computer Control - The "Do Anything" action.
Combines vision, computer control, browser automation, and OCR to perform
ANY task a human can do on a PC via voice command.
"""

import json
import sys
import time
import base64
import subprocess
import platform
import io
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

from PIL import Image

import config
from core.result import ActionResult, action_ok, action_error

def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _get_base_dir()


def _get_api_key() -> str:
    return config.get_api_key()


# ─── DPI Scaling Helpers ──────────────────────────────────────────────────
def scale_coordinates(x: int, y: int, img_width: int, img_height: int) -> Tuple[int, int]:
    """
    Scale screenshot pixel coordinates (img_width x img_height) to PyAutoGUI screen coordinates.
    Corrects for Windows DPI display scaling (e.g. 125%, 150%, 200%).
    """
    try:
        import pyautogui
        screen_w, screen_h = pyautogui.size()
        if not img_width or not img_height:
            return x, y
        scale_x = screen_w / float(img_width)
        scale_y = screen_h / float(img_height)
        return int(round(x * scale_x)), int(round(y * scale_y))
    except Exception:
        return x, y


# ─── Vision Analysis with Gemini ──────────────────────────────────────────
def _analyze_screen_with_gemini(image_bytes: bytes, task: str) -> Dict[str, Any]:
    """Use Gemini to analyze screen and return actionable coordinates/actions."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=_get_api_key())
    model_name = config.get_model("universal_control")

    mime_type = "image/png" if image_bytes.startswith(b"\x89PNG") else "image/jpeg"

    prompt = f"""You are JARVIS's vision system. Analyze this screenshot and determine how to accomplish the task.

TASK: {task}

Return ONLY valid JSON with this structure:
{{
  "action_type": "click|type|hotkey|scroll|drag|wait|none",
  "target": "description of what to interact with",
  "coordinates": {{"x": 100, "y": 200}},  // center of element or drag start
  "to_coordinates": {{"x": 300, "y": 400}}, // drag destination (if action_type=drag)
  "text": "text to type (if action_type=type)",
  "keys": "ctrl+c (if action_type=hotkey)",
  "direction": "down (if action_type=scroll)",
  "confidence": 0.95,
  "reasoning": "why this action",
  "next_step": "what to do after this succeeds"
}}

Guidelines:
- Find the EXACT element matching the task (button, field, link, menu, icon)
- Coordinates must be pixel-perfect center of clickable area relative to image dimensions
- For dragging: provide 'coordinates' for start and 'to_coordinates' for end
- For typing: identify the exact input field, return its coordinates
- For complex tasks: return FIRST step only, set next_step for chaining
- If task is complete: action_type="none", reasoning="done"
- If uncertain: confidence < 0.7, reasoning explains ambiguity"""

    try:
        response = client.models.generate_content(
            model=model_name,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                prompt,
            ],
            config=types.GenerateContentConfig(
                temperature=0.1,
                response_mime_type="application/json",
            ),
        )
        return json.loads(response.text.strip())
    except Exception as e:
        return {"action_type": "none", "error": str(e), "confidence": 0}


def _verify_screen_with_gemini(image_bytes: bytes, original_goal: str, last_action: str) -> Dict[str, Any]:
    """Verify whether the goal has been achieved based on a post-action screenshot."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=_get_api_key())
    model_name = config.get_model("universal_control")
    mime_type = "image/png" if image_bytes.startswith(b"\x89PNG") else "image/jpeg"

    prompt = f"""You are JARVIS's verification system. Inspect this screenshot after executing an action.

ORIGINAL GOAL: {original_goal}
LAST ACTION EXECUTED: {last_action}

Analyze whether the goal has succeeded or what is now needed.
Return ONLY valid JSON:
{{
  "is_complete": true|false,
  "confidence": 0.95,
  "reasoning": "detailed explanation of what is on screen",
  "next_needed_action": "description of next step if not complete, or 'none' if finished"
}}
"""
    try:
        response = client.models.generate_content(
            model=model_name,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                prompt,
            ],
            config=types.GenerateContentConfig(
                temperature=0.1,
                response_mime_type="application/json",
            ),
        )
        return json.loads(response.text.strip())
    except Exception as e:
        return {"is_complete": False, "error": str(e), "reasoning": "Verification call failed"}


# ─── OCR with Tesseract (if available) ────────────────────────────────────
def _ocr_screen(image_bytes: bytes) -> str:
    """Extract all text from screen via OCR."""
    try:
        import pytesseract
        img = Image.open(io.BytesIO(image_bytes))
        return pytesseract.image_to_string(img)
    except Exception:
        return ""


# ─── Computer Control Primitives ──────────────────────────────────────────
def _type_text(text: str, clear_first: bool = True) -> str:
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.02
        if clear_first:
            pyautogui.hotkey("ctrl", "a")
            time.sleep(0.05)
        pyautogui.write(text, interval=0.01)
        return f"Typed: {text[:50]}"
    except Exception as e:
        return f"Type failed: {e}"


def _click_at(x: int, y: int, button: str = "left", clicks: int = 1) -> str:
    try:
        import pyautogui
        pyautogui.moveTo(x, y, duration=0.1)
        pyautogui.click(x, y, button=button, clicks=clicks)
        return f"Clicked at ({x}, {y})"
    except Exception as e:
        return f"Click failed: {e}"


def _press_hotkey(keys: str) -> str:
    try:
        import pyautogui
        key_list = [k.strip().lower() for k in keys.split("+")]
        pyautogui.hotkey(*key_list)
        return f"Pressed: {keys}"
    except Exception as e:
        return f"Hotkey failed: {e}"


def _scroll(direction: str, amount: int = 3) -> str:
    try:
        import pyautogui
        pyautogui.scroll(amount if direction == "up" else -amount)
        return f"Scrolled {direction} {amount}"
    except Exception as e:
        return f"Scroll failed: {e}"


def _drag(x1: int, y1: int, x2: int, y2: int) -> str:
    try:
        import pyautogui
        pyautogui.moveTo(x1, y1, duration=0.1)
        pyautogui.dragTo(x2, y2, duration=0.5, button="left")
        return f"Dragged from ({x1},{y1}) to ({x2},{y2})"
    except Exception as e:
        return f"Drag failed: {e}"


def _focus_window(title: str) -> str:
    try:
        import pygetwindow as gw
        windows = gw.getWindowsWithTitle(title)
        if windows:
            windows[0].activate()
            time.sleep(0.3)
            return f"Focused: {title}"
        return f"Window not found: {title}"
    except Exception as e:
        return f"Focus failed: {e}"


def _screenshot() -> Tuple[bytes, int, int]:
    """Capture screen as bytes and return (bytes, width, height)."""
    try:
        import mss
        import mss.tools
        with mss.mss() as sct:
            monitors = sct.monitors
            target = monitors[1] if len(monitors) > 1 else monitors[0]
            shot = sct.grab(target)
            png = mss.tools.to_png(shot.rgb, shot.size)
            return png, shot.width, shot.height
    except Exception:
        # Fallback
        import pyautogui
        img = pyautogui.screenshot()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        val = buf.getvalue()
        return val, img.width, img.height


# ─── Browser Automation (delegate to browser_control) ─────────────────────
def _browser_action(action: str, **kwargs) -> str:
    """Call browser_control action."""
    try:
        from actions.browser_control import browser_control
        return str(browser_control({"action": action, **kwargs}))
    except Exception as e:
        return f"Browser action failed: {e}"


# ─── Workflow Recording ───────────────────────────────────────────────────
WORKFLOW_FILE = BASE_DIR / "memory" / "workflows.json"


def _save_workflow(name: str, steps: List[Dict]) -> ActionResult:
    workflows = {}
    try:
        if WORKFLOW_FILE.exists():
            workflows = json.loads(WORKFLOW_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    workflows[name] = {"steps": steps, "created": time.time()}
    try:
        WORKFLOW_FILE.parent.mkdir(parents=True, exist_ok=True)
        WORKFLOW_FILE.write_text(json.dumps(workflows, indent=2), encoding="utf-8")
        return action_ok(f"Workflow '{name}' saved ({len(steps)} steps)")
    except Exception as e:
        return action_error(f"Failed to save workflow '{name}': {e}")


def _load_workflow(name: str) -> List[Dict]:
    try:
        if WORKFLOW_FILE.exists():
            return json.loads(WORKFLOW_FILE.read_text(encoding="utf-8")).get(name, {}).get("steps", [])
    except Exception:
        pass
    return []


def _run_workflow(name: str, player=None) -> ActionResult:
    steps = _load_workflow(name)
    if not steps:
        return action_error(f"Workflow '{name}' not found")
    results = []
    for i, step in enumerate(steps):
        if player:
            player.write_log(f"JARVIS: Workflow step {i+1}/{len(steps)}: {step.get('description', step)}")
        result = _execute_step(step)
        results.append(f"Step {i+1}: {result}")
        if "failed" in result.lower() or "error" in result.lower():
            return action_error(f"Workflow '{name}' failed at step {i+1}: {result}", output="\n".join(results))
    return action_ok(f"Workflow '{name}' completed:\n" + "\n".join(results))


def _execute_step(step: Dict) -> str:
    """Execute a single workflow step."""
    action = step.get("action")
    if action == "click":
        return _click_at(step["x"], step["y"], step.get("button", "left"), step.get("clicks", 1))
    elif action == "type":
        return _type_text(step["text"], step.get("clear_first", True))
    elif action == "hotkey":
        return _press_hotkey(step["keys"])
    elif action == "scroll":
        return _scroll(step["direction"], step.get("amount", 3))
    elif action == "drag":
        return _drag(step.get("x1", 0), step.get("y1", 0), step.get("x2", 0), step.get("y2", 0))
    elif action == "wait":
        time.sleep(step.get("seconds", 1))
        return "Waited"
    elif action == "focus":
        return _focus_window(step["title"])
    elif action == "browser":
        return _browser_action(step["browser_action"], **step.get("params", {}))
    return f"Unknown step action: {action}"


# ─── Main Run Function ────────────────────────────────────────────────────
def run(parameters: dict, player=None, session_memory=None) -> ActionResult:
    mode = parameters.get("mode", "execute").lower()

    # ─── Mode: Execute single task via vision with verification ───────────
    if mode == "execute":
        task = parameters.get("task", "").strip()
        max_steps = int(parameters.get("max_steps", 10))
        if not task:
            return action_error("Provide a task (e.g., 'open Chrome and go to github.com', 'click the submit button')")

        if player:
            player.write_log(f"JARVIS: Starting universal task: {task}")

        current_task = task
        for step_num in range(max_steps):
            screen_bytes, img_w, img_h = _screenshot()
            analysis = _analyze_screen_with_gemini(screen_bytes, current_task)

            if "error" in analysis:
                return action_error(f"Vision analysis failed: {analysis['error']}")

            action_type = analysis.get("action_type", "none")
            confidence = analysis.get("confidence", 0)
            reasoning = analysis.get("reasoning", "")

            if player:
                player.write_log(f"JARVIS: Step {step_num+1} - {action_type} ({confidence:.0%}) - {reasoning}")

            if action_type == "none" or confidence < 0.3:
                # Already complete or cannot proceed
                if "done" in reasoning.lower() or "complete" in reasoning.lower():
                    return action_ok(f"Task complete: {reasoning}")
                return action_error(f"Task stopped or uncertain: {reasoning}")

            # Execute action with DPI scaled coordinates
            result_msg = ""
            if action_type == "click":
                raw_coords = analysis.get("coordinates", {})
                x, y = scale_coordinates(raw_coords.get("x", 0), raw_coords.get("y", 0), img_w, img_h)
                result_msg = _click_at(x, y)
            elif action_type == "type":
                raw_coords = analysis.get("coordinates", {})
                if raw_coords:
                    x, y = scale_coordinates(raw_coords.get("x", 0), raw_coords.get("y", 0), img_w, img_h)
                    _click_at(x, y)
                    time.sleep(0.2)
                result_msg = _type_text(analysis.get("text", ""))
            elif action_type == "hotkey":
                result_msg = _press_hotkey(analysis.get("keys", ""))
            elif action_type == "scroll":
                result_msg = _scroll(analysis.get("direction", "down"), 3)
            elif action_type == "drag":
                start_c = analysis.get("coordinates", {})
                end_c = analysis.get("to_coordinates") or analysis.get("end_coordinates", {})
                if start_c and end_c:
                    x1, y1 = scale_coordinates(start_c.get("x", 0), start_c.get("y", 0), img_w, img_h)
                    x2, y2 = scale_coordinates(end_c.get("x", 0), end_c.get("y", 0), img_w, img_h)
                    result_msg = _drag(x1, y1, x2, y2)
                else:
                    result_msg = "Drag failed: missing start or destination coordinates"

            if player:
                player.write_log(f"JARVIS: Action executed: {result_msg}")

            if "failed" in result_msg.lower():
                return action_error(result_msg)

            # Wait briefly for UI to update, then VERIFY with post-action screenshot
            time.sleep(0.6)
            post_bytes, _, _ = _screenshot()
            verification = _verify_screen_with_gemini(post_bytes, task, f"{action_type}: {result_msg}")

            if verification.get("is_complete"):
                return action_ok(f"Verified completed: {verification.get('reasoning', reasoning)}")

            # If not complete, check next steps
            next_step = analysis.get("next_step") or verification.get("next_needed_action")
            if next_step and next_step.lower() != "none" and step_num < max_steps - 1:
                current_task = next_step
                continue

            # If no clear next step and not verified complete, re-evaluate with original task
            if step_num < max_steps - 1:
                current_task = f"{task} (Last action was {result_msg}. Continue.)"
                continue

        return action_error(f"Max steps ({max_steps}) reached without confirming completion. Last status: {reasoning}")

    # ─── Mode: Record workflow ────────────────────────────────────────────
    elif mode == "record":
        name = parameters.get("name", "").strip()
        if not name:
            return action_error("Provide workflow name")
        steps = parameters.get("steps", [])
        return _save_workflow(name, steps)

    # ─── Mode: Run saved workflow ─────────────────────────────────────────
    elif mode == "run_workflow":
        name = parameters.get("name", "").strip()
        if not name:
            return action_error("Provide workflow name")
        return _run_workflow(name, player)

    # ─── Mode: List workflows ─────────────────────────────────────────────
    elif mode == "list_workflows":
        try:
            if not WORKFLOW_FILE.exists():
                return action_ok("No workflows saved.")
            workflows = json.loads(WORKFLOW_FILE.read_text(encoding="utf-8"))
            if not workflows:
                return action_ok("No workflows saved.")
            msg = "Workflows:\n" + "\n".join(f"  • {k} ({len(v.get('steps', []))} steps)" for k, v in workflows.items())
            return action_ok(msg)
        except Exception as e:
            return action_error(f"Error reading workflows: {e}")

    # ─── Mode: OCR screen ─────────────────────────────────────────────────
    elif mode == "ocr":
        screen_bytes, _, _ = _screenshot()
        text = _ocr_screen(screen_bytes)
        if text:
            return action_ok(f"Screen text:\n{text[:3000]}")
        return action_ok("No text found or OCR unavailable")

    # ─── Mode: Dictate into focused field ─────────────────────────────────
    elif mode == "dictate":
        text = parameters.get("text", "").strip()
        if not text:
            return action_error("Provide text to dictate")
        res = _type_text(text)
        return action_ok(res) if "failed" not in res.lower() else action_error(res)

    # ─── Mode: App-specific automation ────────────────────────────────────
    elif mode == "app_action":
        app = parameters.get("app", "").lower()
        action = parameters.get("action", "")
        if app in ("chrome", "edge", "firefox", "browser"):
            res = _browser_action(action, **parameters.get("params", {}))
            return action_ok(res) if "failed" not in res.lower() else action_error(res)
        return action_error(f"App automation for '{app}' not implemented yet")

    # ─── Mode: Screen find (vision-based element location) ────────────────
    elif mode == "find":
        description = parameters.get("description", "").strip()
        if not description:
            return action_error("Provide description of element to find")
        screen_bytes, img_w, img_h = _screenshot()
        analysis = _analyze_screen_with_gemini(screen_bytes, f"Find the exact coordinates of: {description}")
        coords = analysis.get("coordinates", {})
        if coords:
            scaled_x, scaled_y = scale_coordinates(coords.get("x", 0), coords.get("y", 0), img_w, img_h)
            msg = f"Found at screen ({scaled_x}, {scaled_y}) [image ({coords.get('x')}, {coords.get('y')})] - {analysis.get('target', description)} (confidence: {analysis.get('confidence', 0):.0%})"
            return action_ok(msg, coordinates={"x": scaled_x, "y": scaled_y})
        return action_error(f"Not found: {analysis.get('reasoning', 'unknown')}")

    return action_error(f"Unknown mode: {mode}. Use: execute, record, run_workflow, list_workflows, ocr, dictate, app_action, find")


# ── Tool declaration ──────────────────────────────────────────────────────
TOOL = {
    "name": "universal_control",
    "description": "THE master tool - do ANYTHING a human can do on PC via voice. Modes: 'execute' (vision-guided task with verification: 'open Chrome and login to GitHub', 'click the red button', 'fill the form'), 'dictate' (type into focused field), 'find' (locate element: 'find the submit button'), 'ocr' (read all screen text), 'record' (save workflow), 'run_workflow' (execute saved workflow), 'list_workflows', 'app_action' (browser automation).",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "mode": {
                "type": "STRING",
                "description": "execute | dictate | find | ocr | record | run_workflow | list_workflows | app_action"
            },
            "task": {
                "type": "STRING",
                "description": "Natural language task for execute mode"
            },
            "max_steps": {
                "type": "INTEGER",
                "description": "Max vision-action steps for execute (default 10)"
            },
            "text": {
                "type": "STRING",
                "description": "Text for dictate mode"
            },
            "description": {
                "type": "STRING",
                "description": "Element description for find mode"
            },
            "name": {
                "type": "STRING",
                "description": "Workflow name for record/run_workflow"
            },
            "steps": {
                "type": "ARRAY",
                "description": "Workflow steps for record mode",
                "items": {"type": "OBJECT"}
            },
            "app": {
                "type": "STRING",
                "description": "App name for app_action (chrome, edge, etc.)"
            },
            "action": {
                "type": "STRING",
                "description": "Browser action for app_action"
            },
            "params": {
                "type": "OBJECT",
                "description": "Extra params for app_action"
            }
        },
        "required": ["mode"]
    },
    "handler": run,
}