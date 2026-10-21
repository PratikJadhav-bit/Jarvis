"""
Habit Learning & Daily Planner action for JARVIS.
Learns user patterns, suggests automations, and provides daily planning.
"""

import json
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _get_base_dir()
MEMORY_DIR = BASE_DIR / "memory"
HABIT_FILE = MEMORY_DIR / "habits.json"
PLAN_FILE = MEMORY_DIR / "daily_plan.json"


def _load_habits() -> dict:
    try:
        with open(HABIT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"patterns": {}, "suggestions": [], "last_analyzed": 0}


def _save_habits(data: dict) -> None:
    MEMORY_DIR.mkdir(exist_ok=True)
    with open(HABIT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _load_plan() -> dict:
    try:
        with open(PLAN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"date": "", "items": [], "completed": []}


def _save_plan(data: dict) -> None:
    MEMORY_DIR.mkdir(exist_ok=True)
    with open(PLAN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _record_action(action_type: str, details: dict) -> None:
    """Record a user action for habit learning."""
    habits = _load_habits()
    today = datetime.now().strftime("%Y-%m-%d")
    hour = datetime.now().hour

    key = f"{action_type}:{details.get('key', 'general')}"
    if key not in habits["patterns"]:
        habits["patterns"][key] = {"count": 0, "hours": defaultdict(int), "days": defaultdict(int)}

    habits["patterns"][key]["count"] += 1
    habits["patterns"][key]["hours"][str(hour)] += 1
    habits["patterns"][key]["days"][today] += 1

    _save_habits(habits)


def _analyze_patterns() -> list[str]:
    """Analyze patterns and generate automation suggestions."""
    habits = _load_habits()
    suggestions = []

    for key, data in habits["patterns"].items():
        if data["count"] < 5:
            continue

        # Find most common hour
        if data["hours"]:
            top_hour = max(data["hours"].items(), key=lambda x: x[1])[0]
            hour_str = f"{int(top_hour):02d}:00"
            suggestions.append(f"You often {key.replace('_', ' ')} around {hour_str} — automate it?")

        # Find most common day pattern
        if len(data["days"]) >= 3:
            suggestions.append(f"You {key.replace('_', ' ')} frequently — consider a daily routine?")

    # Deduplicate
    seen = set()
    unique = []
    for s in suggestions:
        if s not in seen:
            seen.add(s)
            unique.append(s)

    habits["suggestions"] = unique[:5]
    habits["last_analyzed"] = time.time()
    _save_habits(habits)
    return unique


def _generate_daily_plan() -> dict:
    """Generate a daily plan based on memory, habits, and calendar."""
    from memory.memory_manager import load_memory

    memory = load_memory()
    habits = _load_habits()
    today = datetime.now().strftime("%Y-%m-%d")

    plan = {"date": today, "items": [], "completed": []}

    # Morning briefing items
    plan["items"].append({
        "time": "08:00",
        "title": "Morning Briefing",
        "description": "Time, weather, calendar, top priorities",
        "type": "briefing"
    })

    # Check for meetings from memory/projects
    projects = memory.get("projects", {})
    for key, val in projects.items():
        if isinstance(val, dict) and "meeting" in val.get("value", "").lower():
            plan["items"].append({
                "time": "TBD",
                "title": f"Meeting: {val.get('value', key)}",
                "description": "Check calendar for details",
                "type": "meeting"
            })

    # Habit-based suggestions
    for key, data in habits["patterns"].items():
        if data["count"] >= 3 and data["hours"]:
            top_hour = max(data["hours"].items(), key=lambda x: x[1])[0]
            plan["items"].append({
                "time": f"{int(top_hour):02d}:00",
                "title": f"Habit: {key.replace('_', ' ').title()}",
                "description": f"You usually do this around {int(top_hour):02d}:00",
                "type": "habit"
            })

    _save_plan(plan)
    return plan


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = parameters.get("action", "").lower().strip()

    if action == "record":
        action_type = parameters.get("action_type", "").strip()
        details = parameters.get("details") or {}
        if not action_type:
            return "Provide action_type (e.g., 'mute_slack', 'check_email', 'open_ide')."
        _record_action(action_type, details)
        return f"Recorded habit: {action_type}"

    if action == "analyze":
        suggestions = _analyze_patterns()
        if not suggestions:
            return "Not enough data yet. Keep using JARVIS and I'll learn your patterns."
        return "Automation suggestions based on your habits:\n" + "\n".join(f"• {s}" for s in suggestions)

    if action == "daily_plan":
        plan = _generate_daily_plan()
        if not plan["items"]:
            return "No plan items for today."
        out = f"Daily Plan for {plan['date']}:\n\n"
        for item in plan["items"]:
            out += f"[{item['time']}] {item['title']}\n   {item['description']}\n\n"
        return out.strip()

    if action == "complete":
        item_title = parameters.get("item_title", "").strip()
        if not item_title:
            return "Provide item_title to mark complete."
        plan = _load_plan()
        for item in plan["items"]:
            if item["title"] == item_title:
                plan["items"].remove(item)
                plan["completed"].append({"title": item_title, "completed_at": datetime.now().isoformat()})
                _save_plan(plan)
                return f"Marked complete: {item_title}"
        return f"Item not found: {item_title}"

    if action == "suggestions":
        habits = _load_habits()
        sugg = habits.get("suggestions", [])
        if not sugg:
            return "Run 'analyze' first to generate suggestions."
        return "Current suggestions:\n" + "\n".join(f"• {s}" for s in sugg)

    if action == "morning_briefing":
        # Enhanced morning briefing with plan
        plan = _generate_daily_plan()
        from memory.memory_manager import load_memory
        memory = load_memory()

        now = datetime.now()
        time_str = now.strftime("%A, %B %d, %Y at %I:%M %p")

        out = f"Good morning! It's {time_str}.\n\n"

        # Weather (if available)
        # Could call weather_report action here

        # Calendar/Schedule
        out += "Today's Plan:\n"
        for item in plan["items"]:
            out += f"  {item['time']} - {item['title']}\n"

        # Projects
        projects = memory.get("projects", {})
        active_projects = [v.get("value", k) for k, v in projects.items() if isinstance(v, dict)]
        if active_projects:
            out += f"\nActive Projects: {', '.join(active_projects[:3])}"

        # Habits
        habits = _load_habits()
        if habits.get("suggestions"):
            out += f"\nSuggestion: {habits['suggestions'][0]}"

        return out.strip()

    return f"Unknown action: {action}. Use: record, analyze, daily_plan, complete, suggestions, morning_briefing"


# ── Tool declaration ──────────────────────────────────────────────────────
TOOL = {
    "name": "habit_planner",
    "description": "Learn user habits, suggest automations, and manage daily plans. Actions: 'record' (log an action), 'analyze' (generate automation suggestions), 'daily_plan' (view today's plan), 'complete' (mark plan item done), 'suggestions' (view current suggestions), 'morning_briefing' (enhanced briefing with plan). Use for: 'I mute Slack at 9am', 'analyze my habits', 'what's my plan today', 'mark meeting done'.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "record | analyze | daily_plan | complete | suggestions | morning_briefing"
            },
            "action_type": {
                "type": "STRING",
                "description": "Type of action for record (e.g., 'mute_slack', 'check_email')"
            },
            "details": {
                "type": "OBJECT",
                "description": "Additional context for record action"
            },
            "item_title": {
                "type": "STRING",
                "description": "Title of plan item to complete"
            }
        },
        "required": ["action"]
    },
    "handler": run,
}