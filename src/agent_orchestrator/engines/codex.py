"""OpenAI Codex CLI adapter (`codex exec`).

Measured against codex 0.147.0 — see docs/engine-capabilities.md. The critical detail
is that `codex exec` blocks forever on a non-TTY unless stdin is closed; `worker_cli`
always passes DEVNULL.
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
from agent_orchestrator.prompts import schema_for
from agent_orchestrator.router import AccessLevel, EnginePolicy


BINARY = "codex"

SANDBOX_BY_ACCESS = {
    AccessLevel.READ_ONLY: "read-only",
    AccessLevel.WORKSPACE_WRITE: "workspace-write",
    AccessLevel.FULL: "danger-full-access",
}


class CodexAdapter:
    engine = Engine.CODEX

    def probe(self) -> Capabilities | None:
        path = which(BINARY)
        if path is None:
            return None
        return Capabilities(
            engine=self.engine,
            path=path,
            version=read_version([path, "--version"]),
            structured_output=True,
            reports_cost=False,
            notes=("stdin must be closed or `codex exec` waits for input forever",),
        )

    def build(
        self,
        task: Task,
        policy: EnginePolicy,
        workspace: Path,
        prompt: str,
        artifacts: Path,
    ) -> RunSpec:
        last_message = artifacts / "codex_last_message.md"
        argv = [
            BINARY,
            "exec",
            "--json",
            "--sandbox",
            SANDBOX_BY_ACCESS[policy.effective_access],
            "--cd",
            str(workspace),
            "--skip-git-repo-check",
            "--output-last-message",
            str(last_message),
        ]

        files: dict[Path, str] = {}
        schema = schema_for(task.kind)
        if schema is not None:
            schema_path = artifacts / "codex_schema.json"
            files[schema_path] = json.dumps(schema, indent=2)
            argv += ["--output-schema", str(schema_path)]

        if policy.allow_dangerous:
            argv.append("--dangerously-bypass-approvals-and-sandbox")

        argv.append(prompt)
        return RunSpec(argv=argv, last_message_path=last_message, files=files)

    def parse(self, stdout: str, stderr: str, exit_code: int, spec: RunSpec) -> EngineResult:
        warnings: list[str] = []
        messages: list[str] = []
        session_id: str | None = None
        usage: dict[str, Any] | None = None

        for number, line in enumerate(stdout.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                # A truncated final line is normal when a run is killed; keep the
                # events we did get rather than losing the whole run.
                warnings.append(f"unparseable JSONL on line {number}")
                continue
            if not isinstance(event, dict):
                continue

            match event.get("type"):
                case "thread.started":
                    session_id = event.get("thread_id") or session_id
                case "turn.completed":
                    if isinstance(event.get("usage"), dict):
                        usage = event["usage"]
                case "turn.failed":
                    detail = event.get("error") or event
                    warnings.append(f"turn failed: {json.dumps(detail, ensure_ascii=False)}")
                case "item.completed":
                    item = event.get("item")
                    if isinstance(item, dict) and item.get("type") == "agent_message":
                        text = item.get("text")
                        if isinstance(text, str) and text.strip():
                            messages.append(text)

        # The file written by --output-last-message is the authority: it survives a
        # stream we failed to parse.
        text = _read_last_message(spec.last_message_path)
        if not text:
            text = messages[-1] if messages else ""
        if not text and stderr.strip():
            warnings.append("no agent message; falling back to stderr")
            text = stderr.strip()

        tokens = _normalise_tokens(usage, warnings)
        return EngineResult(
            text=text,
            exit_code=exit_code,
            session_id=session_id,
            usage=usage,
            cost_usd=None,  # codex does not report cost
            warnings=tuple(warnings),
            structured=_maybe_json(text),
            tokens=tokens,
        )


def _read_last_message(path: Path | None) -> str:
    if path is None or not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace").strip()


def _normalise_tokens(
    usage: dict[str, Any] | None, warnings: list[str]
) -> TokenUsage | None:
    if usage is None:
        return None

    input_tokens = read_usage_int(usage, "input_tokens")
    cache_read_tokens = read_usage_int(usage, "cached_input_tokens")
    # ASSUMPTION: Codex follows the OpenAI Responses convention where input includes
    # cached tokens. Treating them as separate would imply 54,967 input tokens for the
    # one-line fixture prompt; no reported total is available to cross-check this.
    if cache_read_tokens > input_tokens:
        warnings.append(
            "codex cached-input convention did not hold; input tokens clamped to zero"
        )

    return TokenUsage(
        input_tokens=max(0, input_tokens - cache_read_tokens),
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=read_usage_int(usage, "cache_write_input_tokens"),
        output_tokens=read_usage_int(usage, "output_tokens"),
        reasoning_tokens=read_usage_int(usage, "reasoning_output_tokens"),
    )


def _maybe_json(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
