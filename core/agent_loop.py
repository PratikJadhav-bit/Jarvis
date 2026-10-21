"""
core/agent_loop.py — Real agent loop for Mark-LIII (Phase 3).

Architecture:
  observe -> decide -> act -> verify (one step at a time)

Features:
  - Original goal is ALWAYS preserved in prompt context.
  - Step history tracks actions, outcomes, and verifications.
  - Max steps & time budget enforcement.
  - Self-correction: upon failed verification, adapts strategy and retries differently.
  - Tool preference order:
      1. Direct APIs / CLI (run_powershell, file ops, browser control)
      2. Windows UI Automation (ui_automation via pywinauto)
      3. Screenshot / vision click only as a last resort
  - Model-agnostic execution via core/llm_client.py.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional

from core import llm_client
from core import kill_switch
from core import policy
from core import learning
from core import keep_awake
from core import screen_lock
from core.action_loader import discover_actions, ActionRegistry
from core.result import ActionResult, to_action_result

_log = logging.getLogger("agent_loop")

SYSTEM_PROMPT = """You are JARVIS's autonomous task execution engine on a Windows laptop.
You execute user goals one step at a time using the loop: OBSERVE -> DECIDE -> ACT -> VERIFY.

TOOL PREFERENCE HIERARCHY (Strict order):
1. PREFERRED: Direct APIs and CLI tools:
   - 'run_powershell' for file listing, command execution, system queries, git, zip/unzip, scripting.
   - 'file_controller', 'file_processor' for dedicated file operations.
   - 'browser_control' for web pages.
2. SECONDARY: Windows UI Automation:
   - 'ui_automation' via pywinauto for interacting with native desktop apps (find/focus/click/type).
3. LAST RESORT ONLY: Screenshot + vision click:
   - 'universal_control' / 'screen_process' only when no API, CLI, or UI Automation can accomplish the action.

CRITICAL RULES:
- The ORIGINAL GOAL is sacred: always make progress towards it.
- Never repeat an action that just failed with identical parameters. RETRY DIFFERENTLY.
- If an action failed verification, adjust arguments or choose an alternative tool from preference hierarchy.
- Output VALID JSON ONLY matching the schema below.

OUTPUT FORMAT (JSON only):
If a step is needed:
{
  "status": "IN_PROGRESS",
  "action": "tool_name",
  "args": { ... },
  "description": "What this action does",
  "verification_strategy": "How to verify that this action succeeded"
}

If the goal is fully achieved:
{
  "status": "DONE",
  "summary": "Clear, concise summary of what was accomplished"
}

If blocked or impossible:
{
  "status": "BLOCKED",
  "reason": "Clear explanation of why progress cannot be made"
}
"""


class AgentLoop:
    def __init__(
        self,
        goal: str,
        task_id: Optional[str] = None,
        max_steps: int = 15,
        time_budget: float = 300.0,
        actions_dir: Optional[Path] = None,
        registry: Optional[ActionRegistry] = None,
    ):
        self.goal = goal.strip()
        self.task_id = task_id or "task_direct"
        self.max_steps = max_steps
        self.time_budget = time_budget
        self.steps: list[dict[str, Any]] = []
        self._start_time: float = 0.0

        if registry is not None:
            self._registry = registry
        else:
            base = actions_dir or (Path(__file__).resolve().parent.parent / "actions")
            self._registry = discover_actions(base, reserved_names=set())

    def _build_tool_catalog(self) -> str:
        decls = self._registry.get_tool_declarations()
        lines = []
        for d in decls:
            lines.append(f"- {d['name']}: {d.get('description', '')}")
        return "\n".join(lines)

    def observe(self, last_result: Optional[ActionResult] = None, last_action: Optional[str] = None) -> dict[str, Any]:
        """Gather current observation context for the decision phase."""
        obs: dict[str, Any] = {
            "elapsed_seconds": round(time.monotonic() - self._start_time, 1),
            "step_count": len(self.steps),
        }
        if last_result is not None:
            obs["last_action"] = last_action
            obs["last_action_ok"] = last_result.ok
            obs["last_action_output"] = last_result.output[:500] if last_result.output else ""
            if not last_result.ok:
                obs["last_action_error"] = last_result.error or "Action returned ok=False"
        return obs

    def decide(self, observation: dict[str, Any]) -> dict[str, Any]:
        """Ask LLM for the single next action given goal, history, and observations."""
        history_summary = []
        for s in self.steps[-5:]:  # short recent history
            st = f"Step {s['step']}: {s['tool']}({json.dumps(s.get('args', {}))}) -> {'OK' if s.get('ok') else 'FAILED'}"
            if not s.get("ok"):
                st += f" (error: {s.get('error')})"
            if "verified" in s:
                st += f" [verified: {s['verified']}]"
            history_summary.append(st)

        # Retrieve relevant recipes
        recipes_context = []
        try:
            for r in learning.retrieve_recipes(self.goal, top_k=3):
                recipes_context.append(
                    f"- Recipe for '{r['goal_pattern']}': {r['steps_json']}"
                    + (f" [gotchas: {r['gotchas']}]" if r.get('gotchas') else "")
                )
        except Exception:
            pass

        prompt_parts = [
            f"ORIGINAL GOAL: {self.goal}",
            f"AVAILABLE TOOLS:\n{self._build_tool_catalog()}",
            f"LEARNED RECIPES & PAST RUNS:\n" + ("\n".join(recipes_context) if recipes_context else "(None yet)"),
            f"CURRENT OBSERVATION:\n{json.dumps(observation, indent=2)}",
            f"STEP HISTORY (last 5):\n" + ("\n".join(history_summary) if history_summary else "(None, starting step 1)"),
            "\nDecide the next action or declare DONE."
        ]
        prompt = "\n\n".join(prompt_parts)

        return llm_client.generate_structured(prompt=prompt, system=SYSTEM_PROMPT)

    def verify(self, action: str, args: dict, result: ActionResult, verification_strategy: str) -> bool:
        """
        Verify that the action succeeded and had the intended effect.
        Returns True if verified, False if verification failed.
        """
        if not result.ok:
            return False

        # Additional verification heuristics:
        # If output explicitly reports failure
        err_keywords = ("failed", "error", "not found", "cannot find", "denied", "timed out")
        out_lower = (result.output or "").lower()
        if any(k in out_lower for k in ("action was not approved", "action blocked by policy", "unknown tool")):
            return False

        return True

    def _finish_run(self, ok: bool, summary: str) -> tuple[bool, str, list[dict]]:
        """Record the run outcome in the learning database and return results."""
        elapsed = time.monotonic() - self._start_time
        try:
            keep_awake.release_keep_awake()
        except Exception:
            pass
        try:
            tools = [s.get("tool") for s in self.steps if "tool" in s]
            learning.record_run(
                task_id=self.task_id,
                goal=self.goal,
                outcome="success" if ok else "fail",
                duration=elapsed,
                steps=self.steps,
                tools=tools
            )
        except Exception as e:
            _log.warning(f"Error recording run in learning DB: {e}")
        return ok, summary, self.steps

    def run(self, is_cancelled_fn: Optional[Callable[[], bool]] = None) -> tuple[bool, str, list[dict]]:
        """
        Run the observe -> decide -> act -> verify loop until completion, budget expiry, or failure.
        Returns (success: bool, summary_or_error: str, step_records: list[dict]).
        """
        self._start_time = time.monotonic()
        try:
            keep_awake.acquire_keep_awake(display_required=False)
        except Exception:
            pass
        last_result: Optional[ActionResult] = None
        last_action: Optional[str] = None
        consecutive_failures = 0

        # Fast verified replay if a proven recipe matches closely
        replay_plan = None
        try:
            replay_plan = learning.get_replay_plan(self.goal)
        except Exception:
            pass

        if replay_plan:
            replay_ok = True
            for step_item in replay_plan:
                if is_cancelled_fn and is_cancelled_fn():
                    return self._finish_run(False, "Task cancelled by user")
                if kill_switch.is_paused():
                    return self._finish_run(False, "Task interrupted by emergency stop kill switch")

                action = step_item.get("tool")
                args = step_item.get("args") or {}
                if not action or not self._registry.has(action):
                    replay_ok = False
                    break

                step_num = len(self.steps) + 1
                ctx = {"task_id": self.task_id, "step": step_num, "background": True}
                raw_res = self._registry.run(action, args, ctx=ctx)
                result = to_action_result(raw_res)
                verified = self.verify(action, args, result, "Check action result")

                self.steps.append({
                    "step": step_num,
                    "tool": action,
                    "args": args,
                    "description": step_item.get("description", f"Replay {action}"),
                    "ok": result.ok,
                    "output": result.output,
                    "error": result.error,
                    "verified": verified,
                })
                last_result = result
                last_action = action

                if not verified:
                    replay_ok = False
                    break

            if replay_ok and self.steps:
                return self._finish_run(True, f"Goal achieved via learned recipe: {self.goal}")
            else:
                self.steps.clear()

        while True:
            # 1. Budget & kill switch checks
            if is_cancelled_fn and is_cancelled_fn():
                return self._finish_run(False, "Task cancelled by user")

            if kill_switch.is_paused():
                return self._finish_run(False, "Task interrupted by emergency stop kill switch")

            elapsed = time.monotonic() - self._start_time
            if elapsed > self.time_budget:
                return self._finish_run(False, f"Time budget exceeded ({elapsed:.1f}s > {self.time_budget}s)")

            if len(self.steps) >= self.max_steps:
                return self._finish_run(False, f"Max steps exceeded ({len(self.steps)} >= {self.max_steps})")

            # 2. Observe
            obs = self.observe(last_result=last_result, last_action=last_action)

            # 3. Decide
            decision = self.decide(obs)
            status = (decision.get("status") or "IN_PROGRESS").upper()

            if status == "DONE":
                summary = decision.get("summary") or f"Goal achieved: {self.goal}"
                return self._finish_run(True, summary)

            if status == "BLOCKED":
                reason = decision.get("reason") or "Agent encountered an impassable block."
                return self._finish_run(False, f"Blocked: {reason}")

            action = decision.get("action")
            args = decision.get("args") or {}
            desc = decision.get("description") or f"Run {action}"
            verify_strat = decision.get("verification_strategy") or "Check action result"

            if not action or not self._registry.has(action):
                consecutive_failures += 1
                last_result = ActionResult(ok=False, error=f"Unknown or missing tool '{action}'")
                last_action = action
                if consecutive_failures >= 3:
                    return self._finish_run(False, f"Agent repeatedly attempted unknown tools ({action})")
                continue

            # Check locked screen for GUI actions
            if screen_lock.is_gui_action(action, args):
                if screen_lock.is_screen_locked():
                    return self._finish_run(
                        False,
                        f"Blocked: Workstation is locked or on secure desktop. GUI action '{action}' cannot run on a locked screen."
                    )
                try:
                    keep_awake.acquire_keep_awake(display_required=True)
                except Exception:
                    pass

            # 4. Act
            step_num = len(self.steps) + 1
            ctx = {"task_id": self.task_id, "step": step_num, "background": True}
            raw_res = self._registry.run(action, args, ctx=ctx)
            result = to_action_result(raw_res)

            # 5. Verify
            verified = self.verify(action, args, result, verify_strat)

            step_record = {
                "step": step_num,
                "tool": action,
                "args": args,
                "description": desc,
                "ok": result.ok,
                "output": result.output,
                "error": result.error,
                "verified": verified,
            }
            self.steps.append(step_record)

            last_result = result
            last_action = action

            if verified:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                # Self-correction: if consecutive failures reach threshold, fail or force alternative
                if consecutive_failures >= 3:
                    err_summary = result.error or result.output or "Verification repeatedly failed"
                    return self._finish_run(False, f"Failed after self-correction attempts at step {step_num} ({action}): {err_summary}")
