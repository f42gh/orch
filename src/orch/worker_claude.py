from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

from orch.config import Config
from orch.db import TaskStore
from orch.guardrails import is_command_blocked
from orch.logging_utils import append_log, task_log_dir
from orch.models import Risk, Task, TaskStatus
from orch.result import save_git_diff, write_result_json
from orch.router import resolve_allowed_tools


def build_prompt(task: Task) -> str:
    high_risk_note = ""
    if task.risk == Risk.HIGH:
        high_risk_note = """

This task is high risk.
Implementing, editing, and deleting are all forbidden.
Do only investigation, blast-radius analysis, an implementation plan, and risk analysis.
"""
    return f"""You are a coding agent running in a local development environment.

## Task
{task.task}

## Working directory
{task.workspace_path}

## Risk level
{task.risk.value}

## Constraints
- Do not modify files outside the cwd
- When something is unclear, investigate the codebase before asking
- Do not perform destructive operations
- Do not read secrets, tokens, or private keys
- Do not run git push
- Do not deploy
- After implementing, run the tests and the linter where you can
- Finish by summarising what changed, how to verify it, and what risk remains{high_risk_note}

## What to output
- What you changed
- The commands you ran
- Test results
- Unresolved problems
- What a human should check
"""


def _build_options(options_class: type[Any], task: Task, can_use_tool: Any | None = None) -> Any:
    kwargs: dict[str, Any] = {
        "cwd": str(task.workspace_path),
        "allowed_tools": resolve_allowed_tools(task.risk),
    }
    if task.risk == Risk.HIGH:
        kwargs["permission_mode"] = "plan"
    elif task.risk == Risk.READ_ONLY:
        kwargs["permission_mode"] = "default"
    else:
        kwargs["permission_mode"] = "acceptEdits"
    if can_use_tool is not None:
        kwargs["can_use_tool"] = can_use_tool

    try:
        signature = inspect.signature(options_class)
    except (TypeError, ValueError):
        return options_class(**kwargs)

    filtered = {key: value for key, value in kwargs.items() if key in signature.parameters}
    return options_class(**filtered)


async def run_claude_task(config: Config, store: TaskStore, task: Task) -> TaskStatus:
    if task.workspace_path is None:
        raise ValueError("task.workspace_path is required")

    log_dir = task_log_dir(config, task.id)
    agent_log = log_dir / "agent.log"
    stdout_log = log_dir / "stdout.log"
    stderr_log = log_dir / "stderr.log"

    try:
        from claude_code_sdk import ClaudeCodeOptions, query
        from claude_code_sdk.types import PermissionResultAllow, PermissionResultDeny
    except ImportError as exc:
        message = (
            "claude-code-sdk is not installed. Install it with "
            "`uv add claude-code-sdk` or ensure it is available in the runtime."
        )
        append_log(stderr_log, message)
        store.update_task(task.id, status=TaskStatus.FAILED, error=message)
        write_result_json(config, task, TaskStatus.FAILED, message, warnings=[str(exc)])
        return TaskStatus.FAILED

    prompt = build_prompt(task)
    append_log(agent_log, f"session_id={task.session_id}")
    append_log(agent_log, f"allowed_tools={resolve_allowed_tools(task.risk)}")
    append_log(agent_log, "prompt:")
    append_log(agent_log, prompt)

    messages: list[str] = []
    status = TaskStatus.NEEDS_REVIEW
    try:
        async def can_use_tool(tool_name: str, tool_input: dict[str, Any], _context: Any) -> Any:
            candidate = tool_input.get("command")
            if candidate is None:
                candidate = json.dumps(tool_input, ensure_ascii=False, sort_keys=True)
            blocked, reason = is_command_blocked(str(candidate))
            if blocked:
                append_log(stderr_log, f"blocked {tool_name}: {reason}: {candidate}")
                return PermissionResultDeny(message=reason or "blocked by guardrails", interrupt=True)
            return PermissionResultAllow()

        options = _build_options(ClaudeCodeOptions, task, can_use_tool=can_use_tool)
        async for message in query(prompt=prompt, options=options):
            rendered = repr(message)
            messages.append(rendered)
            append_log(agent_log, rendered)
            append_log(stdout_log, rendered)
        save_git_diff(config, task.id, Path(task.workspace_path))
        summary = "\n".join(messages[-5:]) if messages else "Claude worker completed without streamed messages."
        write_result_json(config, task, status, summary)
        store.update_task(task.id, status=status, result_summary=summary, error=None)
        return status
    except Exception as exc:
        save_git_diff(config, task.id, Path(task.workspace_path))
        message = f"Claude worker failed: {exc}"
        append_log(stderr_log, message)
        write_result_json(config, task, TaskStatus.FAILED, message)
        store.update_task(task.id, status=TaskStatus.FAILED, error=message)
        return TaskStatus.FAILED
