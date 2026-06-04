from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ._env import child_env

if TYPE_CHECKING:
    from .tools import NormalizedCommandCall


@dataclass
class MiddlewareResult:
    action: Literal["allow", "deny"]
    normalized: "NormalizedCommandCall | None" = None
    reason: str | None = None
    warning: str | None = None


def run_command_middleware(
    middleware_cmd: str,
    *,
    tool_name: str,
    normalized: "NormalizedCommandCall",
    base_dir: str,
    timeout: int,
    is_subagent: bool,
) -> MiddlewareResult:
    """Invoke the command middleware subprocess and parse its response.

    On any failure (missing executable, timeout, bad JSON, non-zero exit),
    returns a MiddlewareResult with action="allow" and a warning (fail-open).
    """
    from .tools import NormalizedCommandCall

    payload = {
        "phase": "before",
        "tool": tool_name,
        "cwd": base_dir,
        "mode": normalized.mode,
        "command": normalized.command,
        "timeout": timeout,
        "is_subagent": is_subagent,
    }

    try:
        argv = shlex.split(middleware_cmd)
    except ValueError as e:
        return MiddlewareResult(
            action="allow",
            warning=f"command_middleware: failed to parse command: {e}",
        )

    try:
        proc = subprocess.run(
            argv,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=10,
            env=child_env(),
        )
    except FileNotFoundError:
        return MiddlewareResult(
            action="allow",
            warning=f"command_middleware: executable not found: {argv[0]}",
        )
    except subprocess.TimeoutExpired:
        return MiddlewareResult(
            action="allow",
            warning="command_middleware: timed out, using original command",
        )
    except OSError as e:
        return MiddlewareResult(
            action="allow",
            warning=f"command_middleware: failed to run: {e}",
        )

    if proc.returncode != 0:
        return MiddlewareResult(
            action="allow",
            warning=(
                f"command_middleware: exited with code {proc.returncode}, "
                "using original command"
            ),
        )

    raw = proc.stdout.strip()
    try:
        response = json.loads(raw)
    except json.JSONDecodeError as e:
        return MiddlewareResult(
            action="allow",
            warning=f"command_middleware: invalid JSON response: {e}",
        )

    if not isinstance(response, dict):
        return MiddlewareResult(
            action="allow",
            warning="command_middleware: response is not a JSON object",
        )

    action = response.get("action")
    if action not in ("allow", "deny"):
        return MiddlewareResult(
            action="allow",
            warning=f"command_middleware: unknown action {action!r}, using original command",
        )

    if action == "deny":
        reason = response.get("reason") or "no reason given"
        return MiddlewareResult(action="deny", reason=reason)

    rewritten_cmd = response.get("command")
    if rewritten_cmd is None:
        return MiddlewareResult(action="allow")

    rewritten_mode = response.get("mode")
    if rewritten_mode not in ("shell", "argv"):
        return MiddlewareResult(
            action="allow",
            warning=f"command_middleware: unknown mode {rewritten_mode!r}, using original command",
        )

    if rewritten_mode == "argv":
        if not isinstance(rewritten_cmd, list) or not all(
            isinstance(s, str) for s in rewritten_cmd
        ):
            return MiddlewareResult(
                action="allow",
                warning="command_middleware: mode=argv command must be a list of strings",
            )
    elif not isinstance(rewritten_cmd, str):
        return MiddlewareResult(
            action="allow",
            warning="command_middleware: mode=shell command must be a string",
        )

    return MiddlewareResult(
        action="allow",
        normalized=NormalizedCommandCall(mode=rewritten_mode, command=rewritten_cmd),
    )
