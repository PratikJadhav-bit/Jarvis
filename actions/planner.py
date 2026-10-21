"""
Planner/Agent action for JARVIS.
Executes multi-step goals by planning tool chains and executing them with verification.
"""

import json
import sys
import re
from pathlib import Path
from typing import Any

import config
from core.result import ActionResult, action_ok, action_error, to_action_result

def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _get_base_dir()


def _get_api_key() -> str:
    return config.get_api_key()


# ─── Planning Prompt ──────────────────────────────────────────────────────
PLANNING_SYSTEM = """You are JARVIS's planning engine. Given a high-level user goal and available tools, create an execution plan.

AVAILABLE TOOLS (name: description):
{tools}

OUTPUT FORMAT — JSON only:
{{
  "plan": [
    {{"step": 1, "tool": "tool_name", "args": {{...}}, "description": "what this step does", "depends_on": []}},
    {{"step": 2, "tool": "tool_name", "args": {{...}}, "description": "what this step does", "depends_on": [1]}},
    ...
  ],
  "reasoning": "Brief explanation of the approach"
}}

RULES:
1. Only use tools from the available list above.
2. Each step must have a valid tool name and args matching that tool's parameters.
3. Use "depends_on" to chain steps — a step waits for its dependencies to complete.
4. Keep plans minimal (3-7 steps max). Prefer fewer, larger steps.
5. If a tool fails, the planner can re-plan (handled by executor).
6. For verification steps, use appropriate read/get_state tools after write actions.
7. NEVER include tools not in the available list.
8. If goal is unclear or impossible with available tools, return {{"plan": [], "reasoning": "Cannot plan: ..."}}
"""


def _build_tool_catalog() -> dict[str, dict]:
    """Get all available tool declarations from action_loader."""
    from core.action_loader import discover_actions
    base = Path(__file__).resolve().parent
    registry = discover_actions(base, reserved_names=set())
    decls = registry.get_tool_declarations()
    return {d["name"]: d for d in decls}


def _plan_with_llm(goal: str, tool_catalog: dict) -> dict:
    """Use Gemini to create a plan."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=_get_api_key())
    model_name = config.get_model("planner")

    tools_desc = "\n".join(f"- {name}: {d['description']}" for name, d in tool_catalog.items())

    prompt = PLANNING_SYSTEM.format(tools=tools_desc) + f"\n\nUSER GOAL: {goal}"

    response = client.models.generate_content(
        model=model_name,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.1,
            response_mime_type="application/json",
        ),
    )

    text = response.text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to extract JSON from markdown
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group())
        return {"plan": [], "reasoning": f"Failed to parse plan: {text[:200]}"}


def _execute_plan(plan: list[dict], player=None, session_memory=None) -> tuple[bool, str, list[dict]]:
    """Execute a plan step by step, returning (success, message, results). Uses structured ok, not exceptions."""
    from core.action_loader import discover_actions
    base = Path(__file__).resolve().parent
    registry = discover_actions(base, reserved_names=set())

    results = []
    step_outputs = {}

    for step in plan:
        step_num = step["step"]
        tool_name = step["tool"]
        args = step.get("args", {})
        depends_on = step.get("depends_on", [])

        # Check dependencies
        for dep in depends_on:
            if dep not in step_outputs:
                return False, f"Step {step_num}: dependency step {dep} not found", results
            if not step_outputs[dep]["success"]:
                return False, f"Step {step_num}: dependency step {dep} failed", results

        # Substitute placeholders from previous steps
        args_str = json.dumps(args)
        for dep in depends_on:
            dep_result = step_outputs[dep]["result"]
            # Simple substitution: {{step_N}} → result of step N
            args_str = args_str.replace(f"{{{{step_{dep}}}}}", str(dep_result))
        args = json.loads(args_str)

        if player:
            player.write_log(f"JARVIS: Executing step {step_num} — {step['description']}")

        if not registry.has(tool_name):
            return False, f"Step {step_num}: unknown tool '{tool_name}'", results

        try:
            raw_result = registry.run(tool_name, args, ctx={"player": player, "session_memory": session_memory})
            result = to_action_result(raw_result)

            if not result.ok:
                err_msg = result.error or result.output or "Action returned ok=False"
                step_outputs[step_num] = {"success": False, "error": err_msg, "result": result.output}
                results.append({"step": step_num, "tool": tool_name, "success": False, "error": err_msg, "result": result.output})
                return False, f"Step {step_num} ({tool_name}) failed: {err_msg}", results

            step_outputs[step_num] = {"success": True, "result": result.output}
            results.append({"step": step_num, "tool": tool_name, "success": True, "result": result.output})
        except Exception as e:
            step_outputs[step_num] = {"success": False, "error": str(e)}
            results.append({"step": step_num, "tool": tool_name, "success": False, "error": str(e)})
            return False, f"Step {step_num} ({tool_name}) threw exception: {e}", results

    return True, "Plan completed successfully", results


def run(parameters: dict, player=None, session_memory=None) -> ActionResult:
    goal = parameters.get("goal", "").strip()
    mode = parameters.get("mode", "execute").lower()  # "plan" | "execute"
    max_retries = int(parameters.get("max_retries", 1))

    if not goal:
        return action_error("Please provide a goal (e.g., 'plan my trip to Tokyo' or 'turn off all lights and lock doors').")

    tool_catalog = _build_tool_catalog()

    if mode == "plan":
        plan_result = _plan_with_llm(goal, tool_catalog)
        plan = plan_result.get("plan", [])
        reasoning = plan_result.get("reasoning", "")
        if not plan:
            return action_error(f"Could not create plan: {reasoning}")
        out_text = "Plan for: " + goal + "\n\n" + json.dumps(plan, indent=2) + "\n\nReasoning: " + reasoning
        return action_ok(out_text, plan=plan, reasoning=reasoning)

    # Execute mode (with optional re-planning on failure)
    for attempt in range(max_retries + 1):
        plan_result = _plan_with_llm(goal, tool_catalog)
        plan = plan_result.get("plan", [])
        reasoning = plan_result.get("reasoning", "")

        if not plan:
            return action_error(f"Could not create plan: {reasoning}")

        if player:
            player.write_log(f"JARVIS: Plan created ({len(plan)} steps). Executing...")

        success, msg, results = _execute_plan(plan, player, session_memory)

        if success:
            summary = f"Goal completed: {goal}\n\n"
            for r in results:
                summary += f"✓ Step {r['step']} ({r['tool']}): {str(r['result'])[:100]}\n"
            return action_ok(summary.strip(), results=results)

        if attempt < max_retries:
            if player:
                player.write_log(f"JARVIS: Plan failed ({msg}). Re-planning (attempt {attempt + 2}/{max_retries + 1})...")
            goal = f"{goal}\n\nPrevious attempt failed: {msg}. Adjust plan."
        else:
            return action_error(f"Goal failed after {max_retries + 1} attempts: {msg}", output=json.dumps(results, indent=2), results=results)

    return action_error("Planner error: unexpected exit")


# ── Tool declaration ──────────────────────────────────────────────────────
TOOL = {
    "name": "planner",
    "description": "Execute multi-step goals by planning and running tool chains. Use for complex requests like: 'plan my trip to Tokyo', 'set up my morning routine', 'turn off all lights, lock doors, and set alarm', 'research and summarize AI news', 'organize my desktop and back up important files'. Mode 'plan' returns the plan without executing; 'execute' runs it with retries.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "goal": {
                "type": "STRING",
                "description": "High-level goal in natural language (e.g., 'book a flight to NYC for Friday')"
            },
            "mode": {
                "type": "STRING",
                "description": "'plan' to see the plan only, 'execute' to run it (default)"
            },
            "max_retries": {
                "type": "INTEGER",
                "description": "Number of re-plan attempts on failure (default 1)"
            }
        },
        "required": ["goal"]
    },
    "handler": run,
}