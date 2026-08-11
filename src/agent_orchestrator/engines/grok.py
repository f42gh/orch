"""xAI Grok Build adapter (`grok -p`).

Measured against grok 1.0.0 — see docs/engine-capabilities.md.

Permission mode is set to always-approve on purpose. The real limit is the kernel
sandbox (Seatbelt on macOS), which holds regardless of permission mode; asking an
unattended process to also negotiate approvals only produces hangs. This is what the
vendor's own guidance for scripts and CI recommends: always-approve plus deny rules
plus a sandbox profile.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_orchestrator.engines.base import (
    Capabilities,
    EngineResult,
    RunSpec,
    read_help,
    read_usage_int,
    read_version,
    which,
)
from agent_orchestrator.models import Engine, Task, TokenUsage
from agent_orchestrator.prompts import schema_for
from agent_orchestrator.router import AccessLevel, EnginePolicy


BINARY = "grok"

SANDBOX_BY_ACCESS = {
    AccessLevel.READ_ONLY: "read-only",
    AccessLevel.WORKSPACE_WRITE: "workspace",
    AccessLevel.FULL: "off",
}


class GrokAdapter:
    engine = Engine.GROK

    def probe(self) -> Capabilities | None:
        path = which(BINARY)
        if path is None:
            return None
        help_text = read_help([path, "--help"])
        return Capabilities(
            engine=self.engine,
            path=path,
            version=read_version([path, "--version"]),
            structured_output="--output-format" in help_text,
            reports_cost=True,
            notes=("cost is omitted entirely when the run reports it as partial",),
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
            "--single",
            prompt,
            "--output-format",
            "json",
            "--cwd",
            str(workspace),
            "--sandbox",
            SANDBOX_BY_ACCESS[policy.effective_access],
            "--permission-mode",
            "bypassPermissions",
            "--max-turns",
            str(policy.max_turns),
            # This orchestrator owns the fan-out. Nested subagents would spend budget
            # outside its accounting and can leave usage totals incomplete.
            "--no-subagents",
            "--no-auto-update",
        ]

        for rule in policy.deny_rules:
            argv += ["--deny", rule]

        schema = schema_for(task.kind)
        if schema is not None:
            # grok takes the schema inline as JSON, not as a file path.
            argv += ["--json-schema", json.dumps(schema)]

        return RunSpec(argv=argv)

    def parse(self, stdout: str, stderr: str, exit_code: int, spec: RunSpec) -> EngineResult:
        warnings: list[str] = []
        payload = _load_last_json_object(stdout)

        if payload is None:
            detail = stderr.strip() or stdout.strip()
            return EngineResult(
                text=detail,
                exit_code=exit_code,
                warnings=("grok emitted no parseable JSON object",),
            )

        if payload.get("type") == "error":
            message = str(payload.get("message") or "grok reported an error")
            return EngineResult(text=message, exit_code=exit_code or 1, warnings=(message,))

        cost_ticks = payload.get("total_cost_usd_ticks")
        if isinstance(cost_ticks, int) and not isinstance(cost_ticks, bool):
            cost = cost_ticks / 10_000_000_000
        else:
            cost = payload.get("total_cost_usd")
        if payload.get("cost_is_partial"):
            # Summing modelUsage rows here would invent a total the server never
            # reported, so drop cost entirely and say why.
            warnings.append("cost reported as partial; cost omitted")
            cost = None
        if payload.get("usage_is_incomplete"):
            warnings.append("usage reported as incomplete; token totals may under-count")

        stop_reason = payload.get("stopReason")
        if stop_reason and stop_reason != "end_turn":
            warnings.append(f"stopped early: {stop_reason}")

        text = str(payload.get("text") or "")
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else None
        return EngineResult(
            text=text,
            exit_code=exit_code,
            session_id=payload.get("sessionId"),
            usage=usage,
            cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
            warnings=tuple(warnings),
            structured=_maybe_json(text),
            tokens=_normalise_tokens(usage),
        )


def _normalise_tokens(usage: dict[str, Any] | None) -> TokenUsage | None:
    if usage is None:
        return None

    # The fixture proves Grok excludes cache reads from input: 2,807 input + 24,960
    # cache reads + 139 output is exactly its reported total of 27,906.
    return TokenUsage(
        input_tokens=read_usage_int(usage, "input_tokens"),
        cache_read_tokens=read_usage_int(usage, "cache_read_input_tokens"),
        cache_write_tokens=read_usage_int(usage, "cache_creation_input_tokens"),
        output_tokens=read_usage_int(usage, "output_tokens"),
        reasoning_tokens=read_usage_int(usage, "reasoning_tokens"),
    )


def _load_last_json_object(stdout: str) -> dict[str, Any] | None:
    """Take the last top-level JSON object on stdout.

    The result object is emitted last, so scanning backwards tolerates any banner or
    stray line printed before it.
    """
    stripped = stdout.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    for line in reversed(stripped.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _maybe_json(text: str) -> dict[str, Any] | None:
    """Read a schema-constrained answer out of the response text.

    A model under a schema sometimes emits one object per turn and they arrive
    concatenated, so a plain `json.loads` fails on the whole string. Decoding
    incrementally and keeping the last complete object recovers the final answer.
    """
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None

    decoder = json.JSONDecoder()
    index = 0
    last: dict[str, Any] | None = None
    while index < len(stripped):
        try:
            parsed, end = decoder.raw_decode(stripped, index)
        except json.JSONDecodeError:
            break
        if isinstance(parsed, dict):
            last = parsed
        index = end
        while index < len(stripped) and stripped[index] in " \t\r\n":
            index += 1
    return last
