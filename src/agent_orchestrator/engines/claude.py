"""Claude Code CLI adapter (`claude -p`).

Used as a worker, not as the orchestrator. It is the fallback for every other engine
because it is the one binary guaranteed to be present: the orchestrator itself runs on it.

This replaces the v0 `worker_claude.py` path, which imported `claude_code_sdk` — a
package that is not installed on this machine, so that worker failed on every run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_orchestrator.engines.base import (
    Capabilities,
    EngineResult,
    RunSpec,
    read_usage_int,
    read_version,
    which,
)
from agent_orchestrator.models import Engine, Task, TokenUsage
from agent_orchestrator.router import AccessLevel, EnginePolicy, resolve_allowed_tools


BINARY = "claude"

PERMISSION_MODE_BY_ACCESS = {
    AccessLevel.READ_ONLY: "plan",
    AccessLevel.WORKSPACE_WRITE: "acceptEdits",
    AccessLevel.FULL: "bypassPermissions",
}


class ClaudeAdapter:
    engine = Engine.CLAUDE

    def probe(self) -> Capabilities | None:
        path = which(BINARY)
        if path is None:
            return None
        return Capabilities(
            engine=self.engine,
            path=path,
            version=read_version([path, "--version"]),
            structured_output=True,
            reports_cost=True,
        )

    def build(
        self,
        task: Task,
        policy: EnginePolicy,
        workspace: Path,
        prompt: str,
        artifacts: Path,
    ) -> RunSpec:
        argv = [
            BINARY,
            "--print",
            prompt,
            "--output-format",
            "json",
            "--add-dir",
            str(workspace),
            "--permission-mode",
            PERMISSION_MODE_BY_ACCESS[policy.effective_access],
            "--max-turns",
            str(policy.max_turns),
            "--allowed-tools",
            ",".join(resolve_allowed_tools(task.risk)),
        ]

        for rule in policy.deny_rules:
            argv += ["--disallowed-tools", rule]

        # Unlike agy, claude honours the process cwd, which `worker_cli` sets to the
        # worktree; --add-dir is belt and braces so the tree is definitely in scope.
        return RunSpec(argv=argv)

    def parse(self, stdout: str, stderr: str, exit_code: int, spec: RunSpec) -> EngineResult:
        warnings: list[str] = []
        payload = _load_json_object(stdout)

        if payload is None:
            return EngineResult(
                text=stderr.strip() or stdout.strip(),
                exit_code=exit_code,
                warnings=("claude emitted no parseable JSON object",),
            )

        if payload.get("is_error"):
            warnings.append(str(payload.get("subtype") or "claude reported an error"))

        denials = payload.get("permission_denials")
        if isinstance(denials, list) and denials:
            # Worth surfacing: it says the agent was blocked, not that it was unable.
            warnings.append(f"{len(denials)} tool call(s) denied by permissions")

        stop_reason = payload.get("stop_reason")
        if stop_reason and stop_reason != "end_turn":
            warnings.append(f"stopped early: {stop_reason}")

        text = str(payload.get("result") or "")
        cost = payload.get("total_cost_usd")
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else None
        return EngineResult(
            text=text,
            exit_code=exit_code,
            session_id=payload.get("session_id"),
            model=_model_name(payload.get("modelUsage")),
            usage=usage,
            cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
            warnings=tuple(warnings),
            structured=_maybe_json(text),
            tokens=_normalise_tokens(usage),
        )


def _model_name(model_usage: object) -> str | None:
    if not isinstance(model_usage, dict) or not all(
        isinstance(name, str) for name in model_usage
    ):
        return None
    names = sorted(model_usage)
    return ",".join(names) if names else None


def _normalise_tokens(usage: dict[str, Any] | None) -> TokenUsage | None:
    if usage is None:
        return None

    # Claude excludes cache reads from input: the fixture reports only 4 input tokens
    # alongside 43,542 cache-read tokens, so there is nothing to subtract.
    return TokenUsage(
        input_tokens=read_usage_int(usage, "input_tokens"),
        cache_read_tokens=read_usage_int(usage, "cache_read_input_tokens"),
        cache_write_tokens=read_usage_int(usage, "cache_creation_input_tokens"),
        output_tokens=read_usage_int(usage, "output_tokens"),
    )


def _load_json_object(stdout: str) -> dict[str, Any] | None:
    stripped = stdout.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _maybe_json(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
