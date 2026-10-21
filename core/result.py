"""
Structured action results for Mark-LIII.
All actions return a structured result: {ok: bool, output: str, error: str|None}.
"""
from __future__ import annotations
from typing import Any, Optional


class ActionResult(dict):
    """
    Structured dictionary returned by all Mark-LIII actions:
    {
        "ok": bool,
        "output": str,
        "error": str | None
    }
    """
    def __init__(self, ok: bool, output: str = "", error: Optional[str] = None, **extra):
        super().__init__(
            ok=bool(ok),
            output=str(output or ""),
            error=str(error) if error is not None else None,
            **extra
        )

    @property
    def ok(self) -> bool:
        return self["ok"]

    @property
    def output(self) -> str:
        return self["output"]

    @property
    def error(self) -> Optional[str]:
        return self["error"]

    @property
    def data(self) -> dict:
        """Returns the dictionary payload of the action result."""
        return self

    def __str__(self) -> str:
        if self["ok"]:
            return self["output"] or "Done."
        return self["error"] or self["output"] or "Failed."

    def __bool__(self) -> bool:
        return self["ok"]


def action_ok(output: str = "Done.", **extra) -> ActionResult:
    """Helper to return a successful action result."""
    return ActionResult(ok=True, output=output, error=None, **extra)


def action_error(error: str, output: str = "", **extra) -> ActionResult:
    """Helper to return an error action result."""
    return ActionResult(ok=False, output=output, error=str(error), **extra)


def to_action_result(value: Any) -> ActionResult:
    """Convert any return value from an action into a normalized ActionResult."""
    if isinstance(value, ActionResult):
        return value
    if isinstance(value, dict) and "ok" in value:
        return ActionResult(
            ok=bool(value["ok"]),
            output=str(value.get("output", "")),
            error=value.get("error"),
            **{k: v for k, v in value.items() if k not in ("ok", "output", "error")}
        )
    if isinstance(value, bool):
        return action_ok("Done.") if value else action_error("Action returned False")
    if value is None:
        return action_ok("Done.")

    text = str(value)
    # Detect common failure patterns from legacy actions returning error strings
    lower = text.lower().strip()
    is_err = any(lower.startswith(p) for p in (
        "failed", "error", "could not", "cannot", "unsupported",
        "unknown mode", "no application name", "window not found"
    ))
    if is_err:
        return action_error(error=text, output=text)
    return action_ok(output=text)
